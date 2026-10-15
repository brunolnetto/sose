"""#504: owner-qualified temporal persistence contract, intentionally RED.

A different organization progressing in the same PostgreSQL namespace must
not replace this organization's recovery boundary. The in-memory test defines
the persistence API before the real two-worker PostgreSQL gate.
"""
from datetime import datetime, timedelta, timezone

import pytest

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

    # Unowned temporal reads are invalid, never interpreted as a legacy namespace.
    with pytest.raises(TypeError):
        store.simulation_position()


def test_scoped_position_writes_do_not_advance_foreign_owner():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.set_simulation_position(_position(1, 1), owner_id="organization-a")
        uow.set_simulation_position(_position(2, 2), owner_id="organization-b")
    with store.transaction() as uow:
        uow.set_simulation_position(_position(3, 3), owner_id="organization-b")
    assert store.simulation_position(owner_id="organization-a") == _position(1, 1)
    assert store.simulation_position(owner_id="organization-b") == _position(3, 3)
    with pytest.raises(TypeError):
        store.simulation_position()


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
        with pytest.raises(TypeError):
            restored.simulation_position()
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
        with pytest.raises(TypeError):
            restored.simulation_position()


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
    a_work = a.schedule(Command("a-cmd", "advance", "demo", "a", ORIGIN + timedelta(hours=3), owner_id="organization-a"))
    b_work = b.schedule(Command("b-cmd", "advance", "demo", "b", ORIGIN + timedelta(hours=3), owner_id="organization-b"))
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


def test_implicit_unowned_runtime_is_forbidden():
    from sose.core.durable import DurableScheduler

    store = MemoryPersistence()
    for constructor in (DurableScheduler, RuntimeRebuilder):
        with pytest.raises(TypeError, match="owner_id"):
            constructor(store)
        with pytest.raises(ValueError, match="owner_id"):
            constructor(store, owner_id=None)
        with pytest.raises(ValueError, match="owner_id"):
            constructor(store, owner_id="")


def test_implicit_unowned_temporal_persistence_is_forbidden():
    from sose.scenarios.model import ScenarioRuntimeState

    store = MemoryPersistence()
    for reader in (store.simulation_position, store.committed_tick, store.scenario_state):
        with pytest.raises(TypeError, match="owner_id"):
            reader()
        with pytest.raises(ValueError, match="owner_id"):
            reader(owner_id=None)

    with store.transaction() as uow:
        for write in (
            lambda: uow.set_simulation_position(_position(1, 1)),
            lambda: uow.set_committed_tick(1),
            lambda: uow.set_scenario_state(ScenarioRuntimeState()),
        ):
            with pytest.raises(TypeError, match="owner_id"):
                write()


def test_committed_ticks_are_owner_scoped_without_scalar_checkpoint():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.set_committed_tick(3, owner_id="organization-a")
        uow.set_committed_tick(9, owner_id="organization-b")
    assert store.committed_tick(owner_id="organization-a") == 3
    assert store.committed_tick(owner_id="organization-b") == 9
    with pytest.raises(TypeError):
        store.committed_tick()
    with store.transaction() as uow:
        uow.set_committed_tick(10, owner_id="organization-b")
    assert store.committed_tick(owner_id="organization-a") == 3
    assert store.committed_tick(owner_id="organization-b") == 10
    with pytest.raises(TypeError):
        store.committed_tick()


def test_owner_committed_ticks_survive_sqlite_restart(tmp_path):
    from sose.persistence.sqlite import SQLitePersistence

    database = tmp_path / "owner-ticks.sqlite"
    with SQLitePersistence(database) as store:
        with store.transaction() as uow:
            uow.set_committed_tick(1, owner_id="organization-a")
            uow.set_committed_tick(4, owner_id="organization-b")

    with SQLitePersistence(database) as restored:
        assert restored.committed_tick(owner_id="organization-a") == 1
        assert restored.committed_tick(owner_id="organization-b") == 4
        with pytest.raises(TypeError):
            restored.committed_tick()


def test_scenario_rebuild_restores_only_selected_owner_snapshot():
    from sose.scenarios.model import ScenarioRuntimeState

    store = MemoryPersistence()
    snapshot = ScenarioRuntimeState()
    with store.transaction() as uow:
        uow.set_scenario_state(snapshot, owner_id="organization-a")

    class Clock:
        now = ORIGIN
        tick = 0

    class Scenarios:
        def __init__(self):
            self.restored = []

        def restore_state(self, state):
            self.restored.append(state)

    class Context:
        def __init__(self):
            self.clock = Clock()
            self.scenarios = Scenarios()

    context_a, context_b = Context(), Context()
    RuntimeRebuilder(store, context=context_a, owner_id="organization-a").rebuild(
        RecordingBackend(ORIGIN), on_due=lambda _: None,
    )
    RuntimeRebuilder(store, context=context_b, owner_id="organization-b").rebuild(
        RecordingBackend(ORIGIN), on_due=lambda _: None,
    )
    assert context_a.scenarios.restored == [snapshot]
    assert context_b.scenarios.restored == [None]
    with pytest.raises(TypeError):
        store.scenario_state()


def test_scheduled_work_owner_cannot_cancel_foreign_work():
    import pytest
    from sose.core.durable import DurableScheduler
    from sose.core.events import Command

    store = MemoryPersistence()
    a = DurableScheduler(store, owner_id="organization-a")
    b = DurableScheduler(store, owner_id="organization-b")
    work = b.schedule(Command("foreign-cmd", "advance", "demo", "same-type", ORIGIN + timedelta(hours=1), owner_id="organization-b"))
    with pytest.raises(PermissionError, match="different owner"):
        a.cancel(work.work_id)
    assert store.command("foreign-cmd") is not None
    assert work in store.scheduled_work()
    assert b.cancel(work.work_id) is True


def test_missing_foreign_command_cannot_poison_unrelated_owner_recovery():
    from sose.core.durable import DurableScheduler
    from sose.core.events import Command

    store = MemoryPersistence()
    owner_a = DurableScheduler(store, owner_id="organization-a")
    owner_b = DurableScheduler(store, owner_id="organization-b")
    own = owner_a.schedule(Command("a-command", "advance", "demo", "a", ORIGIN + timedelta(hours=1), owner_id="organization-a"))
    foreign = owner_b.schedule(Command("b-command", "advance", "demo", "b", ORIGIN + timedelta(hours=2), owner_id="organization-b"))
    with store.transaction() as uow:
        uow.delete_command(foreign.command_id)

    # B's damaged authoritative work must be diagnosed by B, never dispatched
    # or inspected as part of A's independent recovery transaction.
    assert [item.work.work_id for item in owner_a.pending()] == [own.work_id]
    assert RuntimeRebuilder(store, owner_id="organization-a").rebuild(
        RecordingBackend(ORIGIN), on_due=lambda _: None,
    ) == 1
    with pytest.raises(RuntimeError, match="references missing command"):
        owner_b.pending()


def test_scheduled_sequence_is_local_to_owner_not_namespace():
    from sose.core.durable import DurableScheduler
    from sose.core.events import Command

    store = MemoryPersistence()
    a = DurableScheduler(store, owner_id="organization-a")
    b = DurableScheduler(store, owner_id="organization-b")
    aw = a.schedule(Command("a-first", "advance", "demo", "same-type", ORIGIN, owner_id="organization-a"))
    bw = b.schedule(Command("b-first", "advance", "demo", "same-type", ORIGIN, owner_id="organization-b"))
    assert aw.sequence == 1
    assert bw.sequence == 1


def test_identical_same_type_commands_receive_distinct_owner_qualified_ids():
    from sose.factories.command import CommandFactory

    class Target:
        entity_type = "shipment"
        id = "same-chart-entity"

    def factory(owner_id):
        item = CommandFactory(now=lambda: ORIGIN, tick=lambda: 1)
        item.bind_owner(owner_id)
        return item

    a = factory("organization-a").create("pickup", target=Target())
    b = factory("organization-b").create("pickup", target=Target())
    again = factory("organization-a").create("pickup", target=Target())
    assert a.command_id == again.command_id
    assert a.command_id != b.command_id


def test_command_factory_rejects_silent_owner_rebinding():
    from sose.factories.command import CommandFactory

    factory = CommandFactory(now=lambda: ORIGIN, tick=lambda: 1)
    factory.bind_owner("organization-a")
    with pytest.raises(ValueError, match="owner"):
        factory.bind_owner("organization-b")


def test_engine_rejects_foreign_scheduled_callback_before_database_reads():
    from sose.core.durable import DurableScheduledItem
    from sose.core.engine import Engine
    from sose.core.events import Command
    from sose.core.runtime import ScheduledWork

    engine = Engine.__new__(Engine)
    engine.owner_id = "organization-a"
    command = Command("b-cmd", "advance", "demo", "same-type", ORIGIN, owner_id="organization-b")
    work = ScheduledWork(
        work_id="b-work", command_id=command.command_id,
        owner_id="organization-b", due_at=ORIGIN, priority=100, sequence=1,
    )
    # A foreign callback must never reach even the first authoritative read,
    # irrespective of whether its Command/StateChart type matches a local one.
    with pytest.raises(PermissionError, match="different or missing owner"):
        engine._dispatch_scheduled_in_uow(DurableScheduledItem(work, command), object())


def test_scheduled_work_requires_owner_even_when_constructed_directly():
    from sose.core.runtime import ScheduledWork

    values = dict(
        work_id="test-work", command_id="test-command",
        due_at=ORIGIN, priority=100, sequence=1,
    )
    with pytest.raises(TypeError, match="owner_id"):
        ScheduledWork(**values)
    with pytest.raises(ValueError, match="owner_id"):
        ScheduledWork(**values, owner_id=None)
    with pytest.raises(ValueError, match="owner_id"):
        ScheduledWork(**values, owner_id="")
    assert ScheduledWork(**values, owner_id="organization-a").owner_id == "organization-a"


def test_all_scoped_temporal_fields_reject_explicit_none_owner():
    from sose.scenarios.model import ScenarioRuntimeState

    store = MemoryPersistence()
    for reader in (store.simulation_position, store.scenario_state, store.committed_tick):
        with pytest.raises(ValueError, match="owner_id"):
            reader(owner_id=None)

    with store.transaction() as uow:
        actions = (
            lambda: uow.set_simulation_position(_position(1, 1), owner_id=None),
            lambda: uow.set_scenario_state(ScenarioRuntimeState(), owner_id=None),
            lambda: uow.set_committed_tick(1, owner_id=None),
        )
        for action in actions:
            with pytest.raises(ValueError, match="owner_id"):
                action()


@pytest.mark.parametrize(
    "legacy_collection",
    ("simulation_position", "scenario_state", "committed_tick"),
)
def test_historical_scalar_runtime_records_fail_closed(legacy_collection):
    from sose.persistence.records import StateRecord, records_to_state

    old = StateRecord(
        collection=legacy_collection,
        key="__scalar__",
        position=0,
        payload="null",
    )
    with pytest.raises(ValueError, match="unsupported|legacy|unknown"):
        records_to_state((old,))


def test_unknown_durable_record_collection_fails_closed():
    from sose.persistence.records import StateRecord, records_to_state

    with pytest.raises(ValueError, match="unknown|unsupported"):
        records_to_state((StateRecord(
            collection="future_unknown_collection",
            key="something",
            position=0,
            payload="null",
        ),))


def test_owner_scoped_statechart_transitions_get_disjoint_event_ids():
    from sose.factories.event import EventFactory

    class Target:
        entity_type = "shipment"
        id = "same-entity"
        version = 1

    def transition(owner_id):
        factory = EventFactory(now=lambda: ORIGIN, tick=lambda: 1)
        factory.bind_owner(owner_id)
        return factory.transition(
            entity=Target(), trigger="pickup",
            from_state="pending", to_state="picked",
        )

    a = transition("organization-a")
    b = transition("organization-b")
    assert a.event_id != b.event_id
    assert a.event_id == transition("organization-a").event_id


def test_event_factory_cannot_be_reassigned_to_foreign_owner():
    from sose.factories.event import EventFactory

    factory = EventFactory(now=lambda: ORIGIN, tick=lambda: 1)
    factory.bind_owner("organization-a")
    with pytest.raises(ValueError, match="owner"):
        factory.bind_owner("organization-b")


def test_public_scheduled_dispatch_refuses_foreign_owner_before_transaction():
    from sose.core.durable import DurableScheduledItem
    from sose.core.engine import Engine
    from sose.core.events import Command
    from sose.core.runtime import ScheduledWork

    class NoTransactions:
        def transaction(self):
            raise AssertionError("foreign callback must never enter a transaction")

    engine = Engine.__new__(Engine)
    engine.owner_id = "organization-a"
    engine.persistence = NoTransactions()
    command = Command("cmd-b", "advance", "demo", "same-type", ORIGIN, owner_id="organization-b")
    work = ScheduledWork(
        work_id="work-b", command_id=command.command_id,
        due_at=ORIGIN, priority=100, sequence=1,
        owner_id="organization-b",
    )
    with pytest.raises(PermissionError, match="different owner"):
        engine.dispatch_scheduled(DurableScheduledItem(work, command))


def test_tick_refuses_stale_pretransaction_position(monkeypatch):
    from sose.core.clock import SimulationClock
    from sose.core.context import SimulationContext
    from sose.core.engine import Engine
    from sose.core.randomness import RandomSource
    from sose.core.scheduler import Scheduler
    from sose.domain.registry import DomainRegistry

    owner = "organization-a"
    store = MemoryPersistence()
    initial = _position(0, 0)
    other_worker = _position(1, 1)
    with store.transaction() as uow:
        uow.set_simulation_position(initial, owner_id=owner)

    context = SimulationContext(
        clock=SimulationClock(now=ORIGIN, step=timedelta(hours=1), tick=0),
        random=RandomSource(root_seed=9),
        scheduler=Scheduler(),
    )
    engine = Engine(
        context=context, registry=DomainRegistry(),
        persistence=store, owner_id=owner,
    )
    original_transaction = store.transaction
    interleaving = {"injected": False}

    def competing_transaction(*, owner_epoch=None, owner_id=None):
        assert owner_id == owner
        if not interleaving["injected"]:
            interleaving["injected"] = True
            with original_transaction(owner_id=owner) as uow:
                uow.set_simulation_position(other_worker, owner_id=owner)
        return original_transaction(owner_id=owner_id)

    monkeypatch.setattr(store, "transaction", competing_transaction)
    with pytest.raises(RuntimeError, match="stale|changed|position"):
        engine.advance_tick()

    assert store.simulation_position(owner_id=owner) == other_worker
    assert context.clock.now == ORIGIN
    assert context.clock.tick == 0


def test_unowned_in_memory_scheduler_refused_without_durable_effects():
    from sose.core.clock import SimulationClock
    from sose.core.context import SimulationContext
    from sose.core.engine import Engine
    from sose.core.events import Command
    from sose.core.randomness import RandomSource
    from sose.core.scheduler import Scheduler
    from sose.domain.registry import DomainRegistry

    owner = "organization-a"
    store = MemoryPersistence()
    context = SimulationContext(
        clock=SimulationClock(now=ORIGIN, step=timedelta(hours=1), tick=0),
        random=RandomSource(root_seed=5), scheduler=Scheduler(),
    )
    engine = Engine(
        context=context, registry=DomainRegistry(),
        persistence=store, owner_id=owner,
    )
    context.scheduler.schedule(Command(
        command_id="test-unowned-scheduler",
        name="legacy", entity_type="demo", entity_id="one",
        due_at=ORIGIN, owner_id=owner,
    ))
    with pytest.raises(RuntimeError, match="unowned in-memory scheduling"):
        engine.advance_tick()
    assert context.clock.now == ORIGIN
    assert context.clock.tick == 0
    assert store.simulation_position(owner_id=owner) is None
    assert len(context.scheduler) == 1


def test_reference_restart_rejects_whitespace_owner_before_persistence_access():
    from sose.testing.restart import restart_reference_runtime

    with pytest.raises(ValueError, match="owner_id"):
        restart_reference_runtime(
            object(),
            lambda *_args, **_kwargs: None,
            owner_id=" \t ",
            backend_factory=lambda **_kwargs: None,
        )


def test_cancel_rejects_foreign_command_even_when_work_is_labeled_as_owned():
    """A compromised/mismatched work row must never authorize deleting B's command."""
    from sose.core.durable import DurableScheduler
    from sose.core.events import Command
    from sose.core.runtime import ScheduledWork

    store = MemoryPersistence()
    command = Command(
        "command-owned-by-b", "advance", "demo", "same-type",
        ORIGIN + timedelta(hours=1), owner_id="organization-b",
    )
    work = ScheduledWork(
        work_id="misattributed-work-a", command_id=command.command_id,
        due_at=command.due_at, priority=100, sequence=1,
        owner_id="organization-a",
    )
    with store.transaction() as uow:
        uow.save_command(command)
        uow.save_scheduled_work(work)

    scheduler = DurableScheduler(store, owner_id="organization-a")
    with pytest.raises(PermissionError, match="command belongs to a different owner"):
        scheduler.cancel(work.work_id)
    assert store.command(command.command_id) == command
    assert work in store.scheduled_work()


def test_recovery_snapshot_is_read_under_one_owner_scoped_uow():
    """No mixed-generation clock, scenario or scheduled Command reads."""
    from sose.core.events import Command
    from sose.core.runtime import ScheduledWork
    from sose.scenarios.model import ScenarioRuntimeState

    class SnapshotOnlyPersistence(MemoryPersistence):
        def simulation_position(self, *, owner_id):
            raise AssertionError("unfenced position read during rebuild")

        def scenario_state(self, *, owner_id):
            raise AssertionError("unfenced scenario read during rebuild")

        def scheduled_work(self):
            raise AssertionError("split scheduled-work read during rebuild")

    store = SnapshotOnlyPersistence()
    due = ORIGIN + timedelta(hours=2)
    command_a = Command("cmd-a", "advance", "demo", "shared", due, owner_id="organization-a")
    command_b = Command("cmd-b", "advance", "demo", "shared", due, owner_id="organization-b")
    with store.transaction() as uow:
        uow.set_simulation_position(_position(1, 1), owner_id="organization-a")
        uow.set_simulation_position(_position(1, 1), owner_id="organization-b")
        uow.set_scenario_state(ScenarioRuntimeState(), owner_id="organization-a")
        for command in (command_a, command_b):
            uow.save_command(command)
            uow.save_scheduled_work(ScheduledWork(
                work_id=f"work-{command.owner_id}",
                due_at=due,
                priority=100,
                sequence=1,
                command_id=command.command_id,
                owner_id=command.owner_id,
            ))
    backend = RecordingBackend(ORIGIN + timedelta(hours=1))
    rebuilt = RuntimeRebuilder(store, owner_id="organization-a").rebuild(
        backend, on_due=lambda _: None,
    )
    assert rebuilt == 1
    assert [call[2] for call in backend.calls] == [
        ("durable-work", "work-organization-a"),
    ]


@pytest.mark.parametrize("invalid_owner", [None, "", "  "])
def test_durable_domain_events_cannot_be_admitted_without_owner(invalid_owner):
    from sose.core.events import DomainEvent

    store = MemoryPersistence()
    event = DomainEvent(
        event_id="test-unowned-event",
        name="audit.changed",
        entity_type="demo",
        entity_id="one",
        occurred_at=ORIGIN,
        owner_id=invalid_owner,
    )
    with pytest.raises(ValueError, match="operational owner_id"):
        with store.transaction() as uow:
            uow.append_event(event)
    assert store.events() == ()
