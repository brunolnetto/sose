from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity
from sose.persistence.memory import MemoryPersistence


def test_causal_chain_can_be_persisted_and_reconstructed():
    ctx = SimulationContext(
        clock=SimulationClock(
            datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            timedelta(hours=1),
        ),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    store = MemoryPersistence()
    order = ctx.entities.create(Entity, entity_type="order", key=("order-1",))

    created = ctx.events.create("order.created", entity=order, key=("created", order.id))
    approve = ctx.commands.create("approve", target=order, caused_by=created, key=("approve", order.id))
    approved = ctx.events.create("order.approved", entity=order, caused_by=approve, key=("approved", order.id))

    with store.transaction() as uow:
        uow.save_entity(order)
        uow.append_event(created)
        uow.append_event(approved)

    events = store.events()
    assert events[0].event_id == created.event_id
    assert events[1].causation_id == approve.command_id
    assert events[1].correlation_id == created.event_id
