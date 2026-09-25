from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.durable import DurableScheduler
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.mro.entities import WorkOrder
from sose.examples.mro.statecharts import WorkOrderChart
from sose.persistence.memory import MemoryPersistence


def build_runtime():
    now = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1)),
        random=RandomSource(root_seed=42),
        scheduler=Scheduler(),
    )
    persistence = MemoryPersistence()
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))
    work_order = context.entities.create(
        WorkOrder,
        key=("durable-test", 1),
        state="planned",
    )
    with persistence.transaction() as uow:
        uow.save_entity(work_order)
    engine = Engine(context=context, registry=registry, persistence=persistence)
    return now, context, persistence, engine, work_order


def test_dispatch_scheduled_commits_transition_and_consumption_atomically():
    now, context, persistence, engine, work_order = build_runtime()
    command = context.commands.create(
        "release",
        target=work_order,
        due_at=now,
        key=("durable-release", work_order.id),
    )
    scheduler = DurableScheduler(persistence)
    scheduler.schedule(command)
    item = scheduler.pending()[0]

    engine.dispatch_scheduled(item)

    stored = persistence.entity("work_order", work_order.id)
    assert stored is not None
    assert stored.state == "released"
    assert persistence.command(command.command_id) is None
    assert persistence.scheduled_work() == ()
    assert [event.name for event in persistence.events()] == ["work_order.released"]

    position = persistence.simulation_position()
    assert position is not None
    assert position.logical_time == now
    assert position.execution_sequence == 1
    assert position.committed_sequence == 1


def test_dispatch_scheduled_advances_logical_time_to_due_at():
    now, context, persistence, engine, work_order = build_runtime()
    due_at = now + timedelta(hours=2)
    command = context.commands.create(
        "release",
        target=work_order,
        due_at=due_at,
        key=("durable-release-later", work_order.id),
    )
    scheduler = DurableScheduler(persistence)
    scheduler.schedule(command)
    item = scheduler.pending()[0]

    engine.dispatch_scheduled(item)

    assert context.clock.now == due_at
    assert persistence.events()[0].occurred_at == due_at
    assert persistence.simulation_position().logical_time == due_at


def test_failed_scheduled_dispatch_rolls_back_work_and_logical_time():
    now, context, persistence, engine, work_order = build_runtime()
    due_at = now + timedelta(hours=2)
    command = context.commands.create(
        "complete",
        target=work_order,
        due_at=due_at,
        key=("invalid-durable-transition", work_order.id),
    )
    scheduler = DurableScheduler(persistence)
    work = scheduler.schedule(command)
    item = scheduler.pending()[0]

    import pytest

    with pytest.raises(Exception):
        engine.dispatch_scheduled(item)

    stored = persistence.entity("work_order", work_order.id)
    assert stored is not None
    assert stored.state == "planned"
    assert persistence.command(command.command_id) == command
    assert persistence.scheduled_work() == (work,)
    assert persistence.events() == ()
    assert persistence.simulation_position() is None
    assert context.clock.now == now


def test_duplicate_stale_callback_does_not_execute_consumed_work_twice():
    now, context, persistence, engine, work_order = build_runtime()
    command = context.commands.create(
        "release",
        target=work_order,
        due_at=now,
        key=("duplicate-callback", work_order.id),
    )
    scheduler = DurableScheduler(persistence)
    scheduler.schedule(command)
    item = scheduler.pending()[0]

    assert engine.dispatch_scheduled(item) is True
    assert engine.dispatch_scheduled(item) is False

    stored = persistence.entity("work_order", work_order.id)
    assert stored is not None
    assert stored.state == "released"
    assert len(persistence.events()) == 1


def test_stale_callback_cannot_consume_rescheduled_replacement():
    now, context, persistence, engine, work_order = build_runtime()
    original = context.commands.create(
        "release",
        target=work_order,
        due_at=now,
        key=("replacement-callback", work_order.id),
    )
    scheduler = DurableScheduler(persistence)
    scheduler.schedule(original)
    stale_item = scheduler.pending()[0]

    assert engine.dispatch_scheduled(stale_item) is True

    replacement = original.rescheduled(now + timedelta(hours=3))
    replacement_work = scheduler.schedule(replacement)

    assert replacement_work.work_id == stale_item.work.work_id
    assert engine.dispatch_scheduled(stale_item) is False

    assert persistence.command(replacement.command_id) == replacement
    assert persistence.scheduled_work() == (replacement_work,)


def test_engine_binds_schedule_factory_to_durable_scheduler():
    now, context, persistence, engine, work_order = build_runtime()
    command = context.commands.create(
        "release",
        target=work_order,
        key=("schedule-factory-durable", work_order.id),
    )

    scheduled = context.schedules.after(
        hours=2,
        command=command,
        priority=7,
    )

    assert scheduled.due_at == now + timedelta(hours=2)
    assert context.scheduler.due(now + timedelta(hours=2)) == []
    work = persistence.scheduled_work()
    assert len(work) == 1
    assert work[0].command_id == scheduled.command_id
    assert work[0].due_at == scheduled.due_at
    assert work[0].priority == 7


class RecordingTemporalBackend:
    def __init__(self, now):
        self.now = now
        self.calls = []

    def schedule_at(self, at, callback, *, priority=100, key=None):
        self.calls.append((at, priority, key, callback))
        return None

    def create_resource(self, name, *, capacity=1):
        return None

    def request_resource(self, name, *, request_id, on_acquired, priority=100):
        return None


def test_schedule_created_after_rebuild_is_enqueued_on_live_backend():
    now, context, persistence, engine, work_order = build_runtime()
    backend = RecordingTemporalBackend(now)

    assert engine.rebuild_backend(backend) == 0

    command = context.commands.create(
        "release",
        target=work_order,
        key=("live-durable-schedule", work_order.id),
    )
    scheduled = context.schedules.after(hours=2, command=command, priority=7)

    assert len(backend.calls) == 1
    at, priority, key, callback = backend.calls[0]
    assert at == now + timedelta(hours=2)
    assert priority == 7
    assert key == ("durable-work", persistence.scheduled_work()[0].work_id)

    backend.now = at
    callback()

    stored = persistence.entity("work_order", work_order.id)
    assert stored is not None
    assert stored.state == "released"
    assert persistence.scheduled_work() == ()


def test_durable_schedule_executes_on_tick_without_backend_rebuild():
    now, context, persistence, engine, work_order = build_runtime()
    command = context.commands.create(
        "release",
        target=work_order,
        due_at=now,
        key=("tick-durable-schedule", work_order.id),
    )
    context.schedules.at(now, command=command)

    engine.advance_tick()

    stored = persistence.entity("work_order", work_order.id)
    assert stored is not None
    assert stored.state == "released"
    assert persistence.scheduled_work() == ()
    assert len(persistence.events()) == 1


def test_backend_callback_after_tick_execution_is_stale_and_harmless():
    now, context, persistence, engine, work_order = build_runtime()
    backend = RecordingTemporalBackend(now)
    engine.rebuild_backend(backend)

    command = context.commands.create(
        "release",
        target=work_order,
        due_at=now,
        key=("tick-and-backend-durable-schedule", work_order.id),
    )
    context.schedules.at(now, command=command)

    assert len(backend.calls) == 1
    callback = backend.calls[0][3]

    engine.advance_tick()
    assert persistence.entity("work_order", work_order.id).state == "released"
    assert len(persistence.events()) == 1

    assert callback() is False
    assert len(persistence.events()) == 1
