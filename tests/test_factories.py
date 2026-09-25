from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity


def context() -> SimulationContext:
    return SimulationContext(
        clock=SimulationClock(
            now=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            step=timedelta(hours=1),
        ),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )


def test_entity_factory_creates_deterministic_identity():
    a = context().entities.create(Entity, entity_type="order", key=("customer-1", 1))
    b = context().entities.create(Entity, entity_type="order", key=("customer-1", 1))

    assert a.id == b.id
    assert a.entity_type == "order"
    assert a.created_at == datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def test_event_factory_propagates_correlation_and_causation():
    ctx = context()
    entity = ctx.entities.create(Entity, entity_type="order", key=(1,))
    root = ctx.events.create("order.created", entity=entity, key=("root", entity.id))
    command = ctx.commands.create("approve", target=entity, caused_by=root, key=("approve", entity.id))
    event = ctx.events.create("order.approved", entity=entity, caused_by=command, key=("approved", entity.id))

    assert root.correlation_id == root.event_id
    assert command.causation_id == root.event_id
    assert command.correlation_id == root.event_id
    assert event.causation_id == command.command_id
    assert event.correlation_id == root.event_id


def test_transition_event_has_standard_process_metadata():
    ctx = context()
    entity = ctx.entities.create(Entity, entity_type="order", key=(1,))
    command = ctx.commands.create("approve", target=entity, key=("approve", entity.id))
    entity.touch(ctx.clock.now)

    event = ctx.events.transition(
        entity=entity,
        trigger="approve",
        from_state="pending",
        to_state="approved",
        caused_by=command,
    )

    assert event.name == "entity.state_transition"
    assert event.payload == {
        "trigger": "approve",
        "from_state": "pending",
        "to_state": "approved",
    }
    assert event.causation_id == command.command_id
    assert event.correlation_id == command.command_id


def test_schedule_factory_reschedules_and_orders_command():
    ctx = context()
    entity = ctx.entities.create(Entity, entity_type="order", key=(1,))
    command = ctx.commands.create("approve", target=entity, key=("approve", entity.id))

    scheduled = ctx.schedules.after(hours=4, command=command)

    assert scheduled.due_at == ctx.clock.now + timedelta(hours=4)
    assert ctx.scheduler.due(ctx.clock.now) == []
    assert ctx.scheduler.due(ctx.clock.now + timedelta(hours=4)) == [scheduled]
