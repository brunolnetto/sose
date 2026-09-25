from datetime import datetime, timedelta, timezone

from sose.core.events import Command
from sose.core.runtime import ScheduledWork, SimulationPosition
from sose.persistence.memory import MemoryPersistence


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def command(command_id: str, due_at: datetime) -> Command:
    return Command(command_id, "advance", "demo", "1", due_at)


def test_scheduled_work_round_trips_through_persistence():
    store = MemoryPersistence()
    cmd = command("cmd-1", ORIGIN + timedelta(hours=1))
    work = ScheduledWork(
        work_id="work-1",
        due_at=cmd.due_at,
        priority=10,
        sequence=3,
        command_id=cmd.command_id,
    )

    with store.transaction() as uow:
        uow.save_command(cmd)
        uow.save_scheduled_work(work)

    assert store.command("cmd-1") == cmd
    assert store.scheduled_work() == (work,)


def test_scheduled_work_order_is_time_priority_then_sequence():
    store = MemoryPersistence()
    later = command("later", ORIGIN + timedelta(hours=2))
    low = command("low", ORIGIN + timedelta(hours=1))
    high = command("high", ORIGIN + timedelta(hours=1))

    with store.transaction() as uow:
        for cmd, work in (
            (later, ScheduledWork("w-later", later.due_at, 100, 1, later.command_id)),
            (low, ScheduledWork("w-low", low.due_at, 100, 2, low.command_id)),
            (high, ScheduledWork("w-high", high.due_at, 10, 3, high.command_id)),
        ):
            uow.save_command(cmd)
            uow.save_scheduled_work(work)

    assert [w.work_id for w in store.due_scheduled_work(ORIGIN + timedelta(hours=1))] == [
        "w-high",
        "w-low",
    ]


def test_transaction_can_atomically_consume_work_and_commit_position():
    store = MemoryPersistence()
    cmd = command("cmd-1", ORIGIN)
    work = ScheduledWork("work-1", ORIGIN, 100, 1, cmd.command_id)
    position = SimulationPosition(
        logical_time=ORIGIN,
        execution_sequence=7,
        committed_sequence=7,
    )

    with store.transaction() as uow:
        uow.save_command(cmd)
        uow.save_scheduled_work(work)

    with store.transaction() as uow:
        uow.delete_scheduled_work(work.work_id)
        uow.delete_command(cmd.command_id)
        uow.set_simulation_position(position)

    assert store.scheduled_work() == ()
    assert store.command(cmd.command_id) is None
    assert store.simulation_position() == position


def test_rollback_restores_scheduled_work_and_position():
    store = MemoryPersistence()
    cmd = command("cmd-1", ORIGIN)
    work = ScheduledWork("work-1", ORIGIN, 100, 1, cmd.command_id)

    with store.transaction() as uow:
        uow.save_command(cmd)
        uow.save_scheduled_work(work)

    try:
        with store.transaction() as uow:
            uow.delete_scheduled_work(work.work_id)
            uow.delete_command(cmd.command_id)
            uow.set_simulation_position(
                SimulationPosition(ORIGIN, execution_sequence=1, committed_sequence=1)
            )
            raise RuntimeError("crash before commit")
    except RuntimeError:
        pass

    assert store.scheduled_work() == (work,)
    assert store.command(cmd.command_id) == cmd
    assert store.simulation_position() is None
