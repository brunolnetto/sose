"""#504: owner-qualified temporal persistence contract, intentionally RED.

A different organization progressing in the same PostgreSQL namespace must
not replace this organization's recovery boundary. The in-memory test defines
the persistence API before the real two-worker PostgreSQL gate.
"""
from datetime import datetime, timedelta, timezone

from sose.core.durable import RuntimeRebuilder
from sose.core.runtime import SimulationPosition
from sose.persistence.memory import MemoryPersistence


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


class RecordingBackend:
    def __init__(self, now):
        self.now = now
        self.calls = []

    def schedule_at(self, at, callback, *, priority=100, key=None):
        self.calls.append((at, priority, key, callback))


def _position(hours, sequence):
    return SimulationPosition(
        logical_time=ORIGIN + timedelta(hours=hours),
        execution_sequence=sequence,
        committed_sequence=sequence,
        logical_tick=sequence,
    )


def test_same_type_owners_rebuild_at_independent_authoritative_boundaries():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.set_simulation_position(_position(1, 1), owner_id="organization-a")
        uow.set_simulation_position(_position(2, 2), owner_id="organization-b")

    backend_a = RecordingBackend(ORIGIN + timedelta(hours=1))
    backend_b = RecordingBackend(ORIGIN + timedelta(hours=2))
    assert store.simulation_position(owner_id="organization-a") == _position(1, 1)
    assert store.simulation_position(owner_id="organization-b") == _position(2, 2)

    assert RuntimeRebuilder(
        store, owner_id="organization-a",
    ).rebuild(backend_a, on_due=lambda _: None) == 0
    assert RuntimeRebuilder(
        store, owner_id="organization-b",
    ).rebuild(backend_b, on_due=lambda _: None) == 0

    # Explicit legacy reads cannot accidentally alias either scoped owner.
    assert store.simulation_position() is None


def test_scoped_position_writes_do_not_advance_foreign_owner():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.set_simulation_position(_position(1, 1), owner_id="organization-a")
        uow.set_simulation_position(_position(2, 2), owner_id="organization-b")
    with store.transaction() as uow:
        uow.set_simulation_position(_position(3, 3), owner_id="organization-b")
    assert store.simulation_position(owner_id="organization-a") == _position(1, 1)
    assert store.simulation_position(owner_id="organization-b") == _position(3, 3)
    assert store.simulation_position() is None


def test_sqlite_scoped_positions_survive_total_restart_without_legacy_alias(tmp_path):
    from sose.persistence.sqlite import SQLitePersistence

    database = tmp_path / "two-organizations.sqlite"
    store = SQLitePersistence(database)
    try:
        with store.transaction() as uow:
            uow.set_simulation_position(_position(1, 1), owner_id="organization-a")
            uow.set_simulation_position(_position(2, 2), owner_id="organization-b")
    finally:
        store.close()

    restored = SQLitePersistence(database)
    try:
        assert restored.simulation_position(owner_id="organization-a") == _position(1, 1)
        assert restored.simulation_position(owner_id="organization-b") == _position(2, 2)
        assert restored.simulation_position() is None
    finally:
        restored.close()


def test_postgres_scoped_positions_survive_reopen_with_independent_owners():
    import os
    from uuid import uuid4

    import pytest
    from sose.persistence.postgres import PostgresPersistence

    dsn = os.getenv("SOSE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires real PostgreSQL DSN")
    namespace = "owner_clock_" + uuid4().hex[:12]
    with PostgresPersistence(dsn, namespace=namespace) as first:
        with first.transaction() as uow:
            uow.set_simulation_position(_position(1, 1), owner_id="organization-a")
            uow.set_simulation_position(_position(2, 2), owner_id="organization-b")

    with PostgresPersistence(dsn, namespace=namespace) as restored:
        assert restored.simulation_position(owner_id="organization-a") == _position(1, 1)
        assert restored.simulation_position(owner_id="organization-b") == _position(2, 2)
        assert restored.simulation_position() is None


def test_rebuilder_restores_the_selected_owner_clock_into_context():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.set_simulation_position(_position(1, 1), owner_id="organization-a")
        uow.set_simulation_position(_position(2, 2), owner_id="organization-b")

    class Clock:
        now = ORIGIN
        tick = 0

    class Scenarios:
        def restore_state(self, state):
            self.last_state = state

    class Context:
        def __init__(self):
            self.clock = Clock()
            self.scenarios = Scenarios()

    context = Context()
    RuntimeRebuilder(store, owner_id="organization-a", context=context).rebuild(
        RecordingBackend(ORIGIN + timedelta(hours=1)), on_due=lambda _: None,
    )
    assert context.clock.now == ORIGIN + timedelta(hours=1)
    assert context.clock.tick == 1

    context_b = Context()
    RuntimeRebuilder(store, owner_id="organization-b", context=context_b).rebuild(
        RecordingBackend(ORIGIN + timedelta(hours=2)), on_due=lambda _: None,
    )
    assert context_b.clock.now == ORIGIN + timedelta(hours=2)
    assert context_b.clock.tick == 2


def test_same_type_scheduled_work_is_dispatched_only_by_its_owner():
    from sose.core.durable import DurableScheduler
    from sose.core.events import Command

    store = MemoryPersistence()
    a = DurableScheduler(store, owner_id="organization-a")
    b = DurableScheduler(store, owner_id="organization-b")
    a_work = a.schedule(Command("a-cmd", "advance", "demo", "a", ORIGIN + timedelta(hours=3)))
    b_work = b.schedule(Command("b-cmd", "advance", "demo", "b", ORIGIN + timedelta(hours=3)))
    assert a_work.owner_id == "organization-a"
    assert b_work.owner_id == "organization-b"
    seen_a = []
    seen_b = []
    backend_a = RecordingBackend(ORIGIN)
    backend_b = RecordingBackend(ORIGIN)
    assert RuntimeRebuilder(store, owner_id="organization-a").rebuild(
        backend_a, on_due=lambda item: seen_a.append(item.command.command_id)
    ) == 1
    assert RuntimeRebuilder(store, owner_id="organization-b").rebuild(
        backend_b, on_due=lambda item: seen_b.append(item.command.command_id)
    ) == 1
    backend_a.calls[0][3]()
    backend_b.calls[0][3]()
    assert seen_a == ["a-cmd"]
    assert seen_b == ["b-cmd"]
    assert {w.work_id for w in store.scheduled_work()} == {a_work.work_id, b_work.work_id}
