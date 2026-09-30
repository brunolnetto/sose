from datetime import datetime, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.registry import DomainRegistry, EntityType
from sose.domain.warehouse import DomainMutation, MemoryDomainWarehouse
from sose.persistence.memory import MemoryPersistence
from statemachine import State, StateChart

class DemoChart(StateChart):
    planned = State(initial=True)
    released = State(final=True)
    release = planned.to(released)

def test_engine_can_transition_domain_entity_without_engine_entity_shadow():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    persistence = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    entity = Entity('wo-1', 'work_order', state='planned', version=1)
    warehouse.apply(DomainMutation('seed', entity))
    context = SimulationContext(clock=SimulationClock(now=now), random=RandomSource(root_seed=1), scheduler=Scheduler())
    registry = DomainRegistry()
    registry.register(EntityType('work_order', DemoChart))
    engine = Engine(context=context, registry=registry, persistence=persistence, domain_warehouse=warehouse)
    command = context.commands.create('release', target=entity, key=('release', entity.id))
    engine.dispatch(command)
    assert persistence.entity('work_order', 'wo-1') is None
    assert warehouse.entity('work_order', 'wo-1').state == 'planned'
    pending = persistence.domain_deliveries()
    assert len(pending) == 1
    assert pending[0].mutation.entity.state == 'released'
    assert engine.domain_entities.entity('work_order', 'wo-1').state == 'released'
    DomainMutationOutbox(persistence, warehouse).flush()
    assert warehouse.entity('work_order', 'wo-1').state == 'released'
    assert persistence.domain_deliveries() == ()
