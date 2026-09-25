from datetime import datetime, timedelta, timezone

import pytest

from sose.core.durable import DurableScheduler, RuntimeRebuilder
from sose.core.events import Command
from sose.core.runtime import ScheduledWork, SimulationPosition
from sose.persistence.memory import MemoryPersistence


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


class RecordingBackend:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.calls = []

    def schedule_at(self, at, callback, *, priority=100, key=None):
        self.calls.append((at, priority, key, callback))
        return None


def command(command_id: str, due_at: datetime) -> Command:
    return Command(command_id, "advance", "demo", "1", due_at)


def test_durable_scheduler_persists_semantic_work_not_callback():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store)
    cmd = command("cmd-1", ORIGIN + timedelta(hours=1))

    work = scheduler.schedule(cmd, priority=10)

    assert work.command_id == cmd.command_id
    assert work.due_at == cmd.due_at
    assert work.priority == 10
    assert store.command(cmd.command_id) == cmd
    assert store.scheduled_work() == (work,)


def test_durable_scheduler_sequence_survives_reconstruction():
    store = MemoryPersistence()
    first = DurableScheduler(store)
    work1 = first.schedule(command("cmd-1", ORIGIN + timedelta(hours=1)))

    second = DurableScheduler(store)
    work2 = second.schedule(command("cmd-2", ORIGIN + timedelta(hours=1)))

    assert work2.sequence == work1.sequence + 1


def test_rebuilder_reconstructs_backend_events_from_durable_work():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store)
    cmd = command("cmd-1", ORIGIN + timedelta(hours=1))
    work = scheduler.schedule(cmd, priority=7)
    backend = RecordingBackend(now=ORIGIN)
    observed = []

    rebuilt = RuntimeRebuilder(store).rebuild(
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
    scheduler = DurableScheduler(store)
    scheduler.schedule(command("cmd-1", ORIGIN + timedelta(hours=1)))

    with store.transaction() as uow:
        uow.set_simulation_position(
            SimulationPosition(
                logical_time=ORIGIN + timedelta(hours=1),
                execution_sequence=4,
                committed_sequence=4,
            )
        )

    backend = RecordingBackend(now=ORIGIN + timedelta(hours=1))

    assert RuntimeRebuilder(store).rebuild(backend, on_due=lambda _: None) == 1


def test_rebuilder_rejects_pending_work_before_committed_logical_time():
    store = MemoryPersistence()
    scheduler = DurableScheduler(store)
    scheduler.schedule(command("cmd-1", ORIGIN))

    with store.transaction() as uow:
        uow.set_simulation_position(
            SimulationPosition(
                logical_time=ORIGIN + timedelta(hours=1),
                execution_sequence=4,
                committed_sequence=4,
            )
        )

    backend = RecordingBackend(now=ORIGIN + timedelta(hours=1))

    with pytest.raises(RuntimeError, match="before recovery boundary"):
        RuntimeRebuilder(store).rebuild(backend, on_due=lambda _: None)
