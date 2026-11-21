"""Durable resource withdraw and recovery paths (#610 P0 §3).

Covers what ``test_resource_lifecycle.py`` does not: withdrawing a pending or granted
request, finalizing releases interrupted between their durable intent and their
completion, and granting a waiter through a rebuilt backend.

Durable state is written directly, with the record identities
``DurableResourceManager`` itself derives, rather than through ``ensure_requested``:
the manager's own seeding scans the durable state per request (see
``test_resource_lifecycle.py``), and these cases only need the durable truth a
backend is rebuilt from.
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import count
import os

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.identity import deterministic_id
from sose.core.resources import DurableResourceManager
from sose.core.runtime import (
    ResourceDefinition,
    ResourceDemand,
    ResourceReleaseIntent,
    ResourceReservation,
)
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
SIZES = [10, 100] if QUICK else [10, 100, 1_000]
ROUNDS = 3 if QUICK else 7
NOW = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)
RESOURCE = "pool"

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


def _reservation(request_id: str, sequence: int) -> ResourceReservation:
    return ResourceReservation(
        reservation_id=deterministic_id("resource-reservation", RESOURCE, request_id),
        request_id=request_id, resource_name=RESOURCE, acquired_at=NOW, sequence=sequence,
    )


def _seed_durable(persistence, *, capacity: int, holders: int, waiters: int = 0) -> None:
    """Write holders and waiters directly; waiters queue in index order."""
    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition(RESOURCE, capacity=capacity))
        for index in range(holders):
            uow.save_resource_reservation(_reservation(f"holder-{index:05d}", index + 1))
        for index in range(waiters):
            uow.save_resource_demand(ResourceDemand(
                request_id=f"waiter-{index:05d}", resource_name=RESOURCE, priority=index,
                requested_at=NOW, sequence=holders + index + 1,
            ))


def _rebuilt(persistence):
    backend = SimPyBackend(origin=NOW)
    resources = DurableResourceManager(persistence)
    resources.rebuild_backend(backend)
    backend.run_until(NOW)
    return resources, backend


@pytest.mark.parametrize("suffix", ADAPTERS)
@pytest.mark.parametrize("pending", SIZES, ids=lambda size: f"{size}-waiters")
def test_withdraw_a_pending_request(benchmark, tmp_path, suffix, pending):
    """Withdraw one queued request among ``pending`` others; the holder is untouched."""
    persistence = _open(tmp_path, suffix)
    try:
        _seed_durable(persistence, capacity=1, holders=1, waiters=pending)
        resources, backend = _rebuilt(persistence)
        victims = count()

        def enqueue():
            request_id = f"victim-{next(victims)}"
            assert resources.ensure_requested(
                backend, resource_name=RESOURCE, request_id=request_id,
                requested_at=NOW, priority=pending + 1,
            ) is None
            return (request_id,), {}

        withdrawn = benchmark.pedantic(
            lambda request_id: resources.withdraw(backend, request_id),
            setup=enqueue, rounds=ROUNDS, iterations=1,
        )
        assert withdrawn
        assert len(persistence.resource_demands()) == pending
        assert [r.request_id for r in persistence.resource_reservations()] == ["holder-00000"]
    finally:
        _close(persistence)


@pytest.mark.parametrize("suffix", ADAPTERS)
@pytest.mark.parametrize("active", SIZES, ids=lambda size: f"{size}-holders")
def test_withdraw_a_granted_request(benchmark, tmp_path, suffix, active):
    """Withdraw one granted request beside ``active`` other holders (release path)."""
    persistence = _open(tmp_path, suffix)
    try:
        _seed_durable(persistence, capacity=active + 1, holders=active)
        resources, backend = _rebuilt(persistence)
        grants = count()

        def grant():
            request_id = f"granted-{next(grants)}"
            assert resources.ensure_requested(
                backend, resource_name=RESOURCE, request_id=request_id, requested_at=NOW,
            ) is not None
            return (request_id,), {}

        withdrawn = benchmark.pedantic(
            lambda request_id: resources.withdraw(backend, request_id),
            setup=grant, rounds=ROUNDS, iterations=1,
        )
        assert withdrawn
        assert len(persistence.resource_reservations()) == active
        assert persistence.resource_release_intents() == ()
    finally:
        _close(persistence)


@pytest.mark.parametrize("suffix", ADAPTERS)
@pytest.mark.parametrize("size", SIZES, ids=lambda size: f"{size}-holders")
def test_rebuild_finalizes_interrupted_releases(benchmark, tmp_path, suffix, size):
    """Rebuild after a crash left a release intent on a tenth of ``size`` holders.

    Each round restores that durable state outside the timing, then times the
    rebuild, which finalizes every interrupted release before restoring the rest.
    """
    persistence = _open(tmp_path, suffix)
    try:
        _seed_durable(persistence, capacity=size, holders=size)
        interrupted = [_reservation(f"holder-{index:05d}", index + 1)
                       for index in range(0, size, 10)]

        def crash_mid_release():
            with persistence.transaction() as uow:
                for reservation in interrupted:
                    uow.save_resource_reservation(reservation)
                    uow.save_resource_release_intent(ResourceReleaseIntent(
                        intent_id=deterministic_id("resource-release", reservation.reservation_id),
                        reservation_id=reservation.reservation_id, resource_name=RESOURCE,
                        requested_at=NOW,
                    ))
            return (), {}

        def rebuild():
            return DurableResourceManager(persistence).rebuild_backend(SimPyBackend(origin=NOW))

        restored = benchmark.pedantic(rebuild, setup=crash_mid_release, rounds=ROUNDS, iterations=1)
        assert restored == size - len(interrupted)
        assert persistence.resource_release_intents() == ()
        assert len(persistence.resource_reservations()) == size - len(interrupted)
    finally:
        _close(persistence)


@pytest.mark.parametrize("suffix", ADAPTERS)
@pytest.mark.parametrize("pending", SIZES, ids=lambda size: f"{size}-waiters")
def test_release_after_rebuild_grants_the_next_waiter(benchmark, tmp_path, suffix, pending):
    """A fresh process rebuilds, then releases the holder: the first waiter is granted.

    Each round rebuilds outside the timing and queues one more waiter behind the
    others, so every timed release sees a just-rebuilt backend and ``pending`` waiters.
    """
    persistence = _open(tmp_path, suffix)
    try:
        _seed_durable(persistence, capacity=1, holders=1, waiters=pending)
        refills = count(pending)

        def restart():
            with persistence.transaction() as uow:
                index = next(refills)
                uow.save_resource_demand(ResourceDemand(
                    request_id=f"waiter-{index:05d}", resource_name=RESOURCE, priority=index,
                    requested_at=NOW, sequence=10 * pending + index,
                ))
            resources, backend = _rebuilt(persistence)
            [holder] = persistence.resource_reservations()
            return (resources, backend, holder), {}

        def release(resources, backend, holder):
            assert resources.release(backend, holder.reservation_id)
            backend.run_until(NOW)
            return holder.request_id

        benchmark.pedantic(release, setup=restart, rounds=ROUNDS, iterations=1)
        [holder] = persistence.resource_reservations()
        assert holder.request_id.startswith("waiter-")
        assert len(persistence.resource_demands()) == pending
    finally:
        _close(persistence)
