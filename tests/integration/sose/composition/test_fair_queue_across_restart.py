"""#494/#477: a contended resource queue is served in the same order across restarts.

#477 asks that contention "produces durable wait/retry, not terminal failure or double
reservation", and #578 asks for "wait/grant/release and no starvation". The queue's
order lives in the durable ``ResourceDemand`` (priority, then arrival ``sequence``);
SimPy is only an ephemeral arbiter rebuilt from it.

One capacity-1 pool, five waiters with mixed priorities. The **continuous** run grants
them on one backend. In the **restarted** run, every grant decision is made by a
fresh backend and manager rebuilt from durable state. Both must grant in exactly the
same order: the first arrival, then by priority with ties in arrival order. Every
waiter is served (no starvation) and the pool never holds two reservations at once.

A mutant that rebuilds the waiters in another order (here: reversed) turns this RED,
showing the oracle depends on the durable order rather than on a lucky backend.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

T0 = datetime(2026, 11, 26, 8, tzinfo=timezone.utc)
POOL = "dock"
# (request id, priority): lower priority values are served first.
WAITERS = (("a", 100), ("b", 100), ("c", 50), ("d", 100), ("e", 50))
EXPECTED = ["a", "c", "e", "b", "d"]


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
KINDS = ["memory", "sqlite", pytest.param("postgres", marks=pytest.mark.skipif(
    not DSN, reason="SOSE_TEST_POSTGRES_DSN required"))]


@contextmanager
def _store(kind, tmp_path):
    if kind == "postgres":
        from sose.persistence.postgres import PostgresPersistence

        opened = PostgresPersistence(DSN, namespace="fair_queue_" + uuid4().hex[:12])
    elif kind == "sqlite":
        tmp_path.mkdir(parents=True, exist_ok=True)
        opened = SQLitePersistence(tmp_path / "q.db")
    else:
        opened = MemoryPersistence()
    with opened.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition(POOL, capacity=1))
    try:
        yield opened
    finally:
        close = getattr(opened, "close", None)
        if callable(close):
            close()


def _arbiter(store):
    backend = SimPyBackend(origin=T0)
    manager = DurableResourceManager(store)
    manager.rebuild_backend(backend)
    backend.run_until(backend.now)                   # re-acquire the holder's lease
    return manager, backend


def _holder(store):
    reservations = store.resource_reservations()
    assert len(reservations) <= 1, "capacity 1 was exceeded"
    return reservations[0] if reservations else None


def _serve(store, *, restart_each_grant: bool) -> list[str]:
    manager, backend = _arbiter(store)
    for index, (request_id, priority) in enumerate(WAITERS):
        manager.request(backend, resource_name=POOL, request_id=request_id,
                        requested_at=T0 + timedelta(minutes=index), priority=priority)
        backend.run_until(backend.now)
    order = []
    while (holder := _holder(store)) is not None:
        order.append(holder.request_id)
        if restart_each_grant:
            manager, backend = _arbiter(store)       # the next grant uses a rebuilt queue
        manager.release(backend, holder.reservation_id)
        backend.run_until(backend.now)
    assert store.resource_demands() == (), "a waiter was never served"
    return order


@pytest.mark.parametrize("kind", KINDS)
def test_a_rebuilt_queue_grants_in_the_continuous_order(kind, tmp_path):
    with _store(kind, tmp_path / "c") as store:
        continuous = _serve(store, restart_each_grant=False)
    with _store(kind, tmp_path / "r") as store:
        restarted = _serve(store, restart_each_grant=True)
    assert continuous == restarted == EXPECTED


def test_rebuilding_the_queue_in_another_order_is_caught(monkeypatch, tmp_path):
    """RED mutant: the rebuild ignores the durable order of the waiters."""
    original = MemoryPersistence.resource_demands
    monkeypatch.setattr(MemoryPersistence, "resource_demands",
                        lambda self: tuple(reversed(original(self))))
    with _store("memory", tmp_path) as store:
        continuous = _serve(store, restart_each_grant=False)
    with _store("memory", tmp_path) as store:
        restarted = _serve(store, restart_each_grant=True)
    assert continuous == EXPECTED
    # Reversed rebuilds hand the slot to the most recent low-priority waiter first.
    assert restarted == ["a", "e", "c", "d", "b"]
