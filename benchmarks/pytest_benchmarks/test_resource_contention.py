"""Resource contention profiles and operational metrics (#610 P0 §3).

A workload of ``requests`` durable requests arrives at once. Each grant holds its
resource for ``HOLD`` of simulated time and is then released, which hands the
resource to the next waiter, until every request has been served. The timed
callable is the whole workload on a fresh store; its operational counts are
written to ``benchmark.extra_info`` (JSON report) and checked against the profile:

- ``throughput_per_s``: requests served per second of wall time;
- ``wait_p50_slots`` / ``wait_p95_slots`` / ``wait_max_slots``: simulated queue wait,
  in hold periods;
- ``max_queue_length``: the largest number of pending demands;
- ``utilization``: busy capacity over total capacity, averaged over the hold periods;
- ``transactions_per_request``: durable transactions per request lifecycle;
- ``max_waiters_awakened``: grants caused by one hold period's batch of releases.
  Batches are capped by capacity, so this is not a per-release herd-wakeup measure.

Memory only: these are adapter-independent counts plus manager/backend cost. The
adapter cost of each operation is measured in ``test_resource_lifecycle.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count
import os
import time

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
REQUESTS = [24] if QUICK else [24, 96]
DEFINITIONS = [10, 100] if QUICK else [10, 100, 1_000]
ROUNDS = 3 if QUICK else 5
NOW = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)
HOLD = timedelta(minutes=5)

# name: (resources, capacity per resource, priority of request i)
PROFILES = {
    "one-hot-resource": (1, 4, lambda i: 100),
    "evenly-distributed": (4, 1, lambda i: 100),
    "low-capacity": (1, 1, lambda i: 100),
    "high-capacity": (1, 16, lambda i: 100),
    "mixed-priorities": (1, 4, lambda i: (i * 7) % 5),
}


class _CountingStore:
    """Delegate everything; count the durable transactions the manager opens."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.transactions = 0

    def transaction(self, *args, **kwargs):
        self.transactions += 1
        return self._inner.transaction(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def _workload(resources_count: int, capacity: int, priority, requests: int) -> dict:
    inner = MemoryPersistence()
    names = [f"resource-{index}" for index in range(resources_count)]
    with inner.transaction() as uow:
        for name in names:
            uow.save_resource_definition(ResourceDefinition(name, capacity=capacity))
    store = _CountingStore(inner)
    backend = SimPyBackend(origin=NOW)
    manager = DurableResourceManager(store)
    manager.rebuild_backend(backend)
    backend.run_until(NOW)
    store.transactions = 0

    granted_at: dict[str, datetime] = {}

    def on_acquired(reservation):
        granted_at[reservation.request_id] = backend.now

    started = time.perf_counter()
    for index in range(requests):
        if manager.ensure_requested(
            backend, resource_name=names[index % resources_count],
            request_id=f"request-{index:04d}", requested_at=NOW,
            priority=priority(index), on_acquired=on_acquired,
        ) is not None:
            granted_at.setdefault(f"request-{index:04d}", NOW)

    # The driver's own reads are O(state) scans, not manager cost; keep them to one per
    # period.
    max_queue = requests - len(granted_at)
    busy_samples, awakened = [], []
    while holders := inner.resource_reservations():
        busy_samples.append(len(holders))
        backend.run_until(backend.now + HOLD)
        before = len(granted_at)
        for holder in holders:
            assert manager.release(backend, holder.reservation_id)
        backend.run_until(backend.now)
        awakened.append(len(granted_at) - before)
    elapsed = time.perf_counter() - started

    waits = [int((granted_at[f"request-{i:04d}"] - NOW) / HOLD) for i in range(requests)]
    return {
        "throughput_per_s": round(requests / elapsed, 1),
        "wait_p50_slots": _percentile(waits, 0.50),
        "wait_p95_slots": _percentile(waits, 0.95),
        "wait_max_slots": max(waits),
        "max_queue_length": max_queue,
        "utilization": round(
            sum(busy_samples) / (len(busy_samples) * resources_count * capacity), 3,
        ),
        "transactions_per_request": round(store.transactions / requests, 2),
        "max_waiters_awakened": max(awakened, default=0),
        "served": len(granted_at),
        "waits": waits,
    }


@pytest.mark.parametrize("requests", REQUESTS, ids=lambda size: f"{size}-requests")
@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_resource_contention_profile(benchmark, profile, requests):
    resources_count, capacity, priority = PROFILES[profile]
    metrics = benchmark.pedantic(
        _workload, args=(resources_count, capacity, priority, requests),
        rounds=ROUNDS, iterations=1,
    )
    waits = metrics.pop("waits")
    benchmark.extra_info.update(metrics)

    total = resources_count * capacity
    assert metrics["served"] == requests
    # Save demand, commit grant, release intent, finalize release.
    assert metrics["transactions_per_request"] == 4.0
    # Arrivals beyond the total capacity queue; every hold period serves `total`.
    assert metrics["max_queue_length"] == max(0, requests - total)
    assert metrics["wait_max_slots"] == -(-requests // total) - 1
    assert metrics["max_waiters_awakened"] == min(total, max(0, requests - total))
    assert sorted(waits).count(0) == min(requests, total)
    if profile == "mixed-priorities":
        # No preemption: the first `total` arrivals are granted on arrival, whatever
        # their priority. The queue behind them is served lowest priority value first,
        # ties first-in first-out, `total` per hold period.
        queue = sorted(range(total, requests), key=lambda i: (priority(i), i))
        expected = [0] * requests
        for position, index in enumerate(queue):
            expected[index] = 1 + position // total
        assert waits == expected


@pytest.mark.parametrize("suffix", [
    pytest.param(None, id="memory"),
    pytest.param(".sqlite3", id="sqlite-incremental"),
])
@pytest.mark.parametrize("definitions", DEFINITIONS, ids=lambda size: f"{size}-definitions")
def test_request_scales_with_resource_definitions(benchmark, tmp_path, suffix, definitions):
    """One request and withdraw beside ``definitions`` resource definitions.

    ``request`` validates the resource name against every durable definition.
    """
    persistence = (
        MemoryPersistence() if suffix is None
        else SQLiteIncrementalPersistence(tmp_path / f"definitions{suffix}")
    )
    try:
        with persistence.transaction() as uow:
            for index in range(definitions):
                uow.save_resource_definition(ResourceDefinition(f"resource-{index}", capacity=1))
        backend = SimPyBackend(origin=NOW)
        manager = DurableResourceManager(persistence)
        manager.rebuild_backend(backend)
        backend.run_until(NOW)
        target = f"resource-{definitions - 1}"
        calls = count()

        def request_and_withdraw():
            request_id = f"request-{next(calls)}"
            reservation = manager.ensure_requested(
                backend, resource_name=target, request_id=request_id, requested_at=NOW,
            )
            assert manager.withdraw(backend, request_id)
            return reservation

        assert benchmark.pedantic(request_and_withdraw, rounds=ROUNDS, iterations=1) is not None
        assert persistence.resource_reservations() == ()
    finally:
        close = getattr(persistence, "close", None)
        if callable(close):
            close()
