from datetime import datetime, timedelta, timezone

import pytest

from sose.core.durable import DurableScheduler, RecoveryParticipant, RuntimeRebuilder
from sose.core.events import Command
from sose.core.runtime import ScheduledWork, SimulationPosition
from sose.persistence.memory import MemoryPersistence


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNER_ID = "reference-rebuilder"
FOREIGN_OWNER_ID = "independent-organization"


class RecordingBackend:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.calls = []

    def schedule_at(self, at, callback, *, priority=100, key=None):
        self.calls.append((at, priority, key, callback))
        return None


def command(command_id: str, due_at: datetime) -> Command:
    return Command(command_id, "advance", "demo", "1", due_at , owner_id=OWNER_ID)


def test_durable_scheduler_persists_semantic_work_not_callback():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    cmd = command("cmd-1", ORIGIN + timedelta(hours=1))

    work = scheduler.schedule(cmd, priority=10)

    assert work.command_id == cmd.command_id
    assert work.due_at == cmd.due_at
    assert work.priority == 10
    assert store.command(cmd.command_id) == cmd
    assert store.scheduled_work() == (work,)


def test_durable_scheduler_sequence_survives_reconstruction():
    store = MemoryPersistence()
    first = DurableScheduler(store, owner_id=OWNER_ID)
    work1 = first.schedule(command("cmd-1", ORIGIN + timedelta(hours=1)))

    second = DurableScheduler(store, owner_id=OWNER_ID)
    work2 = second.schedule(command("cmd-2", ORIGIN + timedelta(hours=1)))

    assert work2.sequence == work1.sequence + 1


def test_rebuilder_reconstructs_backend_events_from_durable_work():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    cmd = command("cmd-1", ORIGIN + timedelta(hours=1))
    work = scheduler.schedule(cmd, priority=7)
    backend = RecordingBackend(now=ORIGIN)
    observed = []

    rebuilt = RuntimeRebuilder(store, owner_id=OWNER_ID).rebuild(
        backend,
        on_due=lambda item: observed.append((item.work.work_id, item.command.command_id)),
    )

    assert rebuilt == 1
    assert [(at, priority, key) for at, priority, key, _ in backend.calls] == [
        (work.due_at, 7, ("durable-work", work.work_id))
    ]

    backend.calls[0][3]()

    assert observed == [(work.work_id, cmd.command_id)]


def test_rebuilder_uses_persisted_logical_time_as_recovery_boundary():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    scheduler.schedule(command("cmd-1", ORIGIN + timedelta(hours=1)))

    with store.transaction() as uow:
        uow.set_simulation_position(
            SimulationPosition(
                logical_time=ORIGIN + timedelta(hours=1),
                execution_sequence=4,
                committed_sequence=4,
            )
         , owner_id=OWNER_ID)

    backend = RecordingBackend(now=ORIGIN + timedelta(hours=1))

    assert RuntimeRebuilder(store, owner_id=OWNER_ID).rebuild(backend, on_due=lambda _: None) == 1


def test_rebuilder_rejects_pending_work_before_committed_logical_time():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    scheduler.schedule(command("cmd-1", ORIGIN))

    with store.transaction() as uow:
        uow.set_simulation_position(
            SimulationPosition(
                logical_time=ORIGIN + timedelta(hours=1),
                execution_sequence=4,
                committed_sequence=4,
            )
         , owner_id=OWNER_ID)

    backend = RecordingBackend(now=ORIGIN + timedelta(hours=1))

    with pytest.raises(RuntimeError, match="before recovery boundary"):
        RuntimeRebuilder(store, owner_id=OWNER_ID).rebuild(backend, on_due=lambda _: None)


def test_rebuilder_rejects_work_before_backend_time_without_saved_position():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    scheduler.schedule(command("cmd-1", ORIGIN))
    backend = RecordingBackend(now=ORIGIN + timedelta(hours=1))

    with pytest.raises(RuntimeError, match="before recovery boundary"):
        RuntimeRebuilder(store, owner_id=OWNER_ID).rebuild(backend, on_due=lambda _: None)


class FakeClock:
    def __init__(self):
        self.now = ORIGIN
        self.tick = 0


class FakeScenarios:
    def __init__(self):
        self.restored = []

    def restore_state(self, state):
        self.restored.append(state)


class FakeContext:
    def __init__(self):
        self.clock = FakeClock()
        self.scenarios = FakeScenarios()


class FakeResources:
    def __init__(self):
        self.backends = []

    def rebuild_backend(self, backend):
        self.backends.append(backend)
        return 0


def test_rebuilder_restores_runtime_position_scenarios_and_resources():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.set_simulation_position(
            SimulationPosition(
                logical_time=ORIGIN + timedelta(hours=2),
                execution_sequence=3,
                committed_sequence=3,
                logical_tick=9,
            )
         , owner_id=OWNER_ID)

    backend = RecordingBackend(now=ORIGIN + timedelta(hours=2))
    context = FakeContext()
    resources = FakeResources()

    rebuilt = RuntimeRebuilder(
        store,
        owner_id=OWNER_ID,
        context=context,
        participants=(RecoveryParticipant("resources", resources),),
    ).rebuild(
        backend,
        on_due=lambda _: None,
    )

    assert rebuilt == 0
    assert context.clock.now == ORIGIN + timedelta(hours=2)
    assert context.clock.tick == 9
    assert context.scenarios.restored == [None]
    assert resources.backends == [backend]


def test_rebuilder_validates_all_scheduled_work_before_resource_reconstruction():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    scheduler.schedule(command("stale", ORIGIN))

    with store.transaction() as uow:
        uow.set_simulation_position(
            SimulationPosition(
                logical_time=ORIGIN + timedelta(hours=1),
                execution_sequence=1,
                committed_sequence=1,
                logical_tick=1,
            )
         , owner_id=OWNER_ID)

    backend = RecordingBackend(now=ORIGIN + timedelta(hours=1))
    resources = FakeResources()

    with pytest.raises(RuntimeError, match="before recovery boundary"):
        RuntimeRebuilder(
            store,
            owner_id=OWNER_ID,
            context=FakeContext(),
            participants=(RecoveryParticipant("resources", resources),),
        ).rebuild(
            backend,
            on_due=lambda _: None,
        )

    assert resources.backends == []


class OrderedParticipant:
    def __init__(self, name, observed):
        self.name = name
        self.observed = observed

    def validate_rebuild(self):
        self.observed.append(("validate", self.name))

    def rebuild_backend(self, backend):
        self.observed.append(("rebuild", self.name))
        return 0


def test_rebuilder_validates_every_participant_before_any_rebuild():
    store = MemoryPersistence()
    observed = []
    first = OrderedParticipant("first", observed)
    second = OrderedParticipant("second", observed)

    RuntimeRebuilder(
        store,
        owner_id=OWNER_ID,
        participants=(
            RecoveryParticipant("first", first),
            RecoveryParticipant("second", second),
        ),
    ).rebuild(RecordingBackend(now=ORIGIN), on_due=lambda _: None)

    assert observed == [
        ("validate", "first"),
        ("validate", "second"),
        ("rebuild", "first"),
        ("rebuild", "second"),
    ]


def test_rebuilder_exposes_stable_named_recovery_order():
    observed = []
    rebuilder = RuntimeRebuilder(
        MemoryPersistence(),
        owner_id=OWNER_ID,
        context=FakeContext(),
        participants=(
            RecoveryParticipant("resources", OrderedParticipant("resources", observed)),
            RecoveryParticipant("stores", OrderedParticipant("stores", observed)),
            RecoveryParticipant(
                "preemptive-resources",
                OrderedParticipant("preemptive-resources", observed),
            ),
            RecoveryParticipant(
                "containers",
                OrderedParticipant("containers", observed),
            ),
        ),
    )

    assert rebuilder.phase_names == (
        "context",
        "resources",
        "stores",
        "preemptive-resources",
        "containers",
        "scheduled-work",
    )


def test_recovery_participant_rejects_manager_without_rebuild_contract():
    participant = RecoveryParticipant("invalid", object())

    with pytest.raises(TypeError, match="has no rebuild_backend"):
        participant.rebuild(RecordingBackend(now=ORIGIN))


def test_malformed_late_participant_fails_before_any_rebuild():
    observed = []
    valid = OrderedParticipant("valid", observed)

    with pytest.raises(TypeError, match="has no rebuild_backend"):
        RuntimeRebuilder(
            MemoryPersistence(),
            owner_id=OWNER_ID,
            participants=(
                RecoveryParticipant("valid", valid),
                RecoveryParticipant("invalid", object()),
            ),
        ).rebuild(
            RecordingBackend(now=ORIGIN),
            on_due=lambda _: None,
        )

    assert observed == [("validate", "valid")]


def test_rebuilder_restores_scenarios_with_live_clock_when_no_position_exists():
    store = MemoryPersistence()
    context = FakeContext()
    backend = RecordingBackend(now=ORIGIN + timedelta(hours=3))

    rebuilt = RuntimeRebuilder(
        store,
        owner_id=OWNER_ID,
        context=context,
    ).rebuild(
        backend,
        on_due=lambda _: None,
    )

    assert rebuilt == 0
    assert context.clock.now == ORIGIN
    assert context.clock.tick == 0
    assert context.scenarios.restored == [None]


def test_scheduler_attached_backend_schedules_and_detach_stops_callbacks():
    store = MemoryPersistence()
    backend = RecordingBackend(now=ORIGIN)
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    observed = []
    scheduler.attach_backend(
        backend,
        on_due=lambda item: observed.append(item.command.command_id),
    )

    first = scheduler.schedule(command("cmd-attached", ORIGIN + timedelta(minutes=30)))
    assert [(at, key) for at, _, key, _ in backend.calls] == [
        (first.due_at, ("durable-work", first.work_id))
    ]

    backend.calls[0][3]()
    assert observed == ["cmd-attached"]

    scheduler.detach_backend()
    scheduler.schedule(command("cmd-detached", ORIGIN + timedelta(minutes=45)))
    assert len(backend.calls) == 1


def test_scheduler_rejects_duplicate_and_past_due_work_with_attached_backend():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    scheduler.schedule(command("cmd-1", ORIGIN + timedelta(minutes=5)))

    with pytest.raises(ValueError, match="already scheduled"):
        scheduler.schedule(command("cmd-1", ORIGIN + timedelta(minutes=15)))

    scheduler.attach_backend(RecordingBackend(now=ORIGIN + timedelta(minutes=10)), on_due=lambda _: None)
    with pytest.raises(ValueError, match="before backend logical time"):
        scheduler.schedule(command("cmd-past", ORIGIN + timedelta(minutes=9)))


def test_scheduler_find_pending_due_and_cancel_pending_paths():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    early = scheduler.schedule(command("cmd-early", ORIGIN + timedelta(minutes=5)))
    scheduler.schedule(
        Command(
            "cmd-late",
            "other",
            "demo",
            "2",
            ORIGIN + timedelta(hours=1),
         owner_id=OWNER_ID)
    )

    assert [item.work.work_id for item in scheduler.due(ORIGIN + timedelta(minutes=30))] == [
        early.work_id
    ]

    assert (
        scheduler.find_pending(
            entity_type="demo",
            entity_id="1",
            name="missing",
        )
        is None
    )
    assert scheduler.cancel_pending(
        entity_type="demo",
        entity_id="1",
        name="missing",
    ) is False
    assert scheduler.cancel_pending(
        entity_type="demo",
        entity_id="1",
        name="advance",
    ) is True
    assert scheduler.cancel_pending(
        entity_type="demo",
        entity_id="1",
        name="advance",
    ) is False


def test_scheduler_find_pending_rejects_duplicate_semantic_matches():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    scheduler.schedule(command("cmd-1", ORIGIN + timedelta(minutes=1)))
    scheduler.schedule(command("cmd-2", ORIGIN + timedelta(minutes=2)))

    with pytest.raises(RuntimeError, match="multiple pending scheduled commands"):
        scheduler.find_pending(
            entity_type="demo",
            entity_id="1",
            name="advance",
        )


def test_scheduler_pending_and_cancel_reject_missing_command_reference():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    work = scheduler.schedule(command("cmd-1", ORIGIN + timedelta(minutes=1)))
    with store.transaction() as uow:
        uow.delete_command("cmd-1")

    with pytest.raises(RuntimeError, match="references missing command"):
        scheduler.pending()
    with pytest.raises(RuntimeError, match="references missing command"):
        scheduler.cancel(work.work_id)


def test_scheduler_cancel_returns_false_when_target_not_found():
    scheduler = DurableScheduler(MemoryPersistence(), owner_id=OWNER_ID)
    assert scheduler.cancel("missing-work") is False


def test_recovery_participant_validate_and_rebuild_normalize_return_type():
    class Manager:
        def __init__(self):
            self.validated = False
            self.rebuilt = []

        def validate_rebuild(self):
            self.validated = True

        def rebuild_backend(self, backend):
            self.rebuilt.append(backend)
            return None

    manager = Manager()
    participant = RecoveryParticipant("demo", manager)

    participant.validate()
    rebuilt = participant.rebuild(RecordingBackend(now=ORIGIN))

    assert manager.validated is True
    assert rebuilt == 0
    assert len(manager.rebuilt) == 1


def test_rebuilder_filters_foreign_scheduled_work_without_consuming_it():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    own = Command("own-mro", "release", "work_order", "wo-1", ORIGIN + timedelta(hours=2) , owner_id=OWNER_ID)
    foreign = Command("foreign-shipment", "schedule_pickup", "shipment", "s-1",
                      ORIGIN + timedelta(hours=1), owner_id=FOREIGN_OWNER_ID)
    own_work = scheduler.schedule(own)
    foreign_work = DurableScheduler(store, owner_id=FOREIGN_OWNER_ID).schedule(foreign)

    backend = RecordingBackend(ORIGIN)
    observed = []
    rebuilt = RuntimeRebuilder(
        store,
        owner_id=OWNER_ID, scheduled_work_filter=lambda item: item.command.entity_type == "work_order",
    ).rebuild(backend, on_due=lambda item: observed.append(item.command.command_id))

    assert rebuilt == 1
    assert [entry[2] for entry in backend.calls] == [
        ("durable-work", own_work.work_id),
    ]
    backend.calls[0][3]()
    assert observed == ["own-mro"]
    assert set(store.scheduled_work()) == {own_work, foreign_work}
    assert store.command(foreign.command_id) == foreign


def test_rebuilder_filters_foreign_earlier_work_before_own_recovery_boundary_validation():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store, owner_id=OWNER_ID)
    foreign = DurableScheduler(store, owner_id=FOREIGN_OWNER_ID).schedule(Command("older-foreign", "pickup", "shipment", "s-1", ORIGIN, owner_id=FOREIGN_OWNER_ID))
    own = scheduler.schedule(Command(
        "current-own", "release", "work_order", "wo-1", ORIGIN + timedelta(hours=2),
     owner_id=OWNER_ID))
    with store.transaction() as uow:
        uow.set_simulation_position(SimulationPosition(
            logical_time=ORIGIN + timedelta(hours=1), execution_sequence=1,
            committed_sequence=1,
        ) , owner_id=OWNER_ID)
    backend = RecordingBackend(ORIGIN + timedelta(hours=1))
    assert RuntimeRebuilder(
        store,
        owner_id=OWNER_ID, scheduled_work_filter=lambda item: item.command.entity_type == "work_order",
    ).rebuild(backend, on_due=lambda _: None) == 1
    assert backend.calls[0][2] == ("durable-work", own.work_id)
    assert foreign in store.scheduled_work()


def test_rebuilder_does_not_read_another_organizations_position():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.set_simulation_position(
            SimulationPosition(
                logical_time=ORIGIN + timedelta(hours=3),
                execution_sequence=3,
                committed_sequence=3,
                logical_tick=3,
            ), owner_id=OWNER_ID,
        )
        uow.set_simulation_position(
            SimulationPosition(
                logical_time=ORIGIN,
                execution_sequence=0,
                committed_sequence=0,
            ), owner_id=FOREIGN_OWNER_ID,
        )
    backend = RecordingBackend(now=ORIGIN)
    assert RuntimeRebuilder(store, owner_id=FOREIGN_OWNER_ID).rebuild(
        backend, on_due=lambda _: None,
    ) == 0
    assert backend.calls == []
