"""Ordinary durable resource lifecycle costs as reservations and demands grow (#610 P0 §3).

``DurableResourceManager`` answers ``has_request`` and ``reservation_for`` by scanning
the durable reservations and demands, so these benchmarks vary the number of active
holders and pending waiters independently of the operation being measured.
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import count
import os

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
# Every ensure_requested still scans the durable reservations, so seeding grows
# quadratically, but since the committed reads stopped deep-copying the immutable
# resource records (#610) 1_000 holders seed in about a second on memory (tens of
# seconds per case on SQLite incremental, for the scheduled full run only).
SIZES = [10, 50] if QUICK else [10, 100, 1_000]
# Three rounds even in quick mode, so the hand-over assertions cover several hand-overs.
ROUNDS = 3 if QUICK else 7
NOW = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)

ADAPTERS = (
    pytest.param(None, id="memory"),
    pytest.param(".sqlite3", id="sqlite-incremental"),
)


def _open(tmp_path, suffix):
    if suffix is None:
        return MemoryPersistence()
    return SQLiteIncrementalPersistence(tmp_path / f"resources{suffix}")


def _close(persistence):
    close = getattr(persistence, "close", None)
    if callable(close):
        close()


def _runtime(persistence, *, capacity: int):
    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("pool", capacity=capacity))
    backend = SimPyBackend(origin=NOW)
    resources = DurableResourceManager(persistence)
    resources.rebuild_backend(backend)
    backend.run_until(NOW)
    return resources, backend


def _request(resources, backend, request_id: str, *, priority: int = 100):
    return resources.ensure_requested(
        backend, resource_name="pool", request_id=request_id,
        requested_at=backend.now, priority=priority,
    )


def _seed(resources, backend, *, holders: int, waiters: int = 0) -> None:
    for index in range(holders):
        assert _request(resources, backend, f"holder-{index:05d}") is not None
    for index in range(waiters):
        assert _request(resources, backend, f"waiter-{index:05d}", priority=index) is None


@pytest.mark.parametrize("suffix", ADAPTERS)
@pytest.mark.parametrize("active", SIZES, ids=lambda size: f"{size}-holders")
def test_resource_lookup_scales_with_active_reservations(benchmark, tmp_path, suffix, active):
    """Lookups of a held id and of an id that is neither held nor pending.

    Every lookup materialises all reservations before scanning, so the position of
    the held id in scan order does not matter.
    """
    persistence = _open(tmp_path, suffix)
    try:
        resources, backend = _runtime(persistence, capacity=active)
        _seed(resources, backend, holders=active)
        last = f"holder-{active - 1:05d}"

        def lookups():
            return (
                resources.has_request(last),
                resources.reservation_for(last),
                resources.has_request("absent"),
            )

        held, reservation, absent = benchmark.pedantic(lookups, rounds=ROUNDS, iterations=1)
        assert held and reservation is not None and not absent
    finally:
        _close(persistence)


@pytest.mark.parametrize("suffix", ADAPTERS)
@pytest.mark.parametrize("pending", SIZES, ids=lambda size: f"{size}-waiters")
def test_resource_lookup_scales_with_pending_demands(benchmark, tmp_path, suffix, pending):
    """``has_request`` falls through to the demand scan for a queued request."""
    persistence = _open(tmp_path, suffix)
    try:
        resources, backend = _runtime(persistence, capacity=1)
        _seed(resources, backend, holders=1, waiters=pending)
        last = f"waiter-{pending - 1:05d}"

        result = benchmark.pedantic(
            lambda: (resources.has_request(last), resources.reservation_for(last)),
            rounds=ROUNDS, iterations=1,
        )
        assert result == (True, None)
    finally:
        _close(persistence)


@pytest.mark.parametrize("suffix", ADAPTERS)
@pytest.mark.parametrize("background", SIZES, ids=lambda size: f"{size}-background-holders")
def test_resource_immediate_grant_then_release(benchmark, tmp_path, suffix, background):
    """One durable request granted immediately and released, beside N other holders."""
    persistence = _open(tmp_path, suffix)
    try:
        resources, backend = _runtime(persistence, capacity=background + 1)
        _seed(resources, backend, holders=background)
        ids = count()

        def cycle():
            reservation = _request(resources, backend, f"cycle-{next(ids)}")
            released = resources.release(backend, reservation.reservation_id)
            backend.run_until(backend.now)
            return reservation, released

        reservation, released = benchmark.pedantic(cycle, rounds=ROUNDS, iterations=1)
        assert reservation is not None and released
        assert len(persistence.resource_reservations()) == background
    finally:
        _close(persistence)


@pytest.mark.parametrize("suffix", ADAPTERS)
@pytest.mark.parametrize("pending", SIZES, ids=lambda size: f"{size}-waiters")
def test_resource_release_grants_the_next_waiter(benchmark, tmp_path, suffix, pending):
    """Releasing the single holder durably grants the highest-priority waiter.

    Each round first queues one more waiter (behind all others, outside the timing),
    so every measured hand-over sees exactly ``pending`` waiters.
    """
    persistence = _open(tmp_path, suffix)
    try:
        resources, backend = _runtime(persistence, capacity=1)
        _seed(resources, backend, holders=1, waiters=pending)
        refills = count(pending)
        handed_over = []

        def refill():
            index = next(refills)
            assert _request(resources, backend, f"waiter-{index:05d}", priority=index) is None
            [holder] = persistence.resource_reservations()
            return (holder,), {}

        def hand_over(holder):
            assert resources.release(backend, holder.reservation_id)
            backend.run_until(backend.now)
            handed_over.append(holder.request_id)

        benchmark.pedantic(hand_over, setup=refill, rounds=ROUNDS, iterations=1)
        done = len(handed_over)          # one call when benchmarking is disabled
        [holder] = persistence.resource_reservations()
        assert holder.request_id == f"waiter-{done - 1:05d}"    # lowest priority value first
        assert len(persistence.resource_demands()) == pending
    finally:
        _close(persistence)


@pytest.mark.parametrize("suffix", ADAPTERS)
@pytest.mark.parametrize("size", SIZES, ids=lambda size: f"{size}-holders-and-waiters")
def test_resource_backend_rebuild(benchmark, tmp_path, suffix, size):
    """Reconstruct an ephemeral backend from N durable holders and N pending demands."""
    persistence = _open(tmp_path, suffix)
    try:
        resources, backend = _runtime(persistence, capacity=size)
        _seed(resources, backend, holders=size, waiters=size)

        def rebuild():
            fresh = SimPyBackend(origin=NOW)
            restored = DurableResourceManager(persistence).rebuild_backend(fresh)
            fresh.run_until(NOW)
            return restored

        assert benchmark.pedantic(rebuild, rounds=ROUNDS, iterations=1) == 2 * size
        assert len(persistence.resource_reservations()) == size
        assert len(persistence.resource_demands()) == size
    finally:
        _close(persistence)
