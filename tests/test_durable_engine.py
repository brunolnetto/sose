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
