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
from datetime import datetime, timezone
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


class _Durable:
    """The durable store under test; ``reopen`` drops the connection and opens anew.

    Memory has no connection to reopen, so its "restart" keeps the same state
    object; SQLite and PostgreSQL reopen from their path or DSN.
    """

    def __init__(self, kind, tmp_path):
        self.kind = kind
        self.path = tmp_path / "q.db"
        self.namespace = "fair_queue_" + uuid4().hex[:12]
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.store = self._open()
        with self.store.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition(POOL, capacity=1))

    def _open(self):
        if self.kind == "postgres":
            from sose.persistence.postgres import PostgresPersistence

            return PostgresPersistence(DSN, namespace=self.namespace)
        if self.kind == "sqlite":
            return SQLitePersistence(self.path)
        return MemoryPersistence()

    def reopen(self):
        if self.kind != "memory":
            self.close()
            self.store = self._open()
        return self.store

    def close(self):
        close = getattr(self.store, "close", None)
        if callable(close):
            close()


@contextmanager
def _durable(kind, tmp_path):
    durable = _Durable(kind, tmp_path)
    try:
        yield durable
    finally:
        durable.close()


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


def _serve(durable, *, restart_each_grant: bool) -> list[str]:
    store = durable.store
    manager, backend = _arbiter(store)
    for request_id, priority in WAITERS:
        # Same requested_at for all: the durable arrival ``sequence`` breaks ties.
        manager.request(backend, resource_name=POOL, request_id=request_id,
                        requested_at=T0, priority=priority)
        backend.run_until(backend.now)
    order = []
    while (holder := _holder(store)) is not None:
        order.append(holder.request_id)
        if restart_each_grant:
            store = durable.reopen()                 # a new connection and process state
            manager, backend = _arbiter(store)       # the next grant uses a rebuilt queue
        manager.release(backend, holder.reservation_id)
        backend.run_until(backend.now)
    assert store.resource_demands() == (), "a waiter was never served"
    return order


@pytest.mark.parametrize("kind", KINDS)
def test_a_rebuilt_queue_grants_in_the_continuous_order(kind, tmp_path):
    with _durable(kind, tmp_path / "c") as durable:
        continuous = _serve(durable, restart_each_grant=False)
    with _durable(kind, tmp_path / "r") as durable:
        restarted = _serve(durable, restart_each_grant=True)
    assert continuous == restarted == EXPECTED


def test_rebuilding_the_queue_in_another_order_is_caught(monkeypatch, tmp_path):
    """RED mutant: the rebuild ignores the durable order of the waiters."""
    original = MemoryPersistence.resource_demands
    monkeypatch.setattr(MemoryPersistence, "resource_demands",
                        lambda self: tuple(reversed(original(self))))
    with _durable("memory", tmp_path / "c") as durable:
        continuous = _serve(durable, restart_each_grant=False)
    with _durable("memory", tmp_path / "r") as durable:
        restarted = _serve(durable, restart_each_grant=True)
    assert continuous == EXPECTED
    # Reversed rebuilds hand the slot to the most recent low-priority waiter first.
    assert restarted == ["a", "e", "c", "d", "b"]
