from sose.domain.delivery import DomainDelivery
from sose.domain.entity import Entity
from sose.domain.entity_store import WarehouseBackedEntityStore
from sose.domain.warehouse import DomainMutation, MemoryDomainWarehouse
from sose.persistence.memory import MemoryPersistence

def _entity(version, state):
    return Entity(id='wo-1', entity_type='work_order', state=state, version=version)

def test_warehouse_entity_is_read_when_no_pending_mutation():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    warehouse.apply(DomainMutation('v1', _entity(1, 'planned')))
    assert WarehouseBackedEntityStore(engine, warehouse).entity('work_order', 'wo-1').state == 'planned'

def test_pending_committed_mutation_overrides_stale_warehouse():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    warehouse.apply(DomainMutation('v1', _entity(1, 'planned')))
    with engine.transaction() as uow:
        uow.save_domain_delivery(DomainDelivery(DomainMutation('v2', _entity(2, 'released'))))
    current = WarehouseBackedEntityStore(engine, warehouse).entity('work_order', 'wo-1')
    assert current.version == 2
    assert current.state == 'released'

def test_newer_warehouse_version_wins_over_older_pending_delivery():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    warehouse.apply(DomainMutation('v2', _entity(2, 'released')))
    with engine.transaction() as uow:
        uow.save_domain_delivery(DomainDelivery(DomainMutation('v1', _entity(1, 'planned'))))
    current = WarehouseBackedEntityStore(engine, warehouse).entity('work_order', 'wo-1')
    assert current.version == 2
    assert current.state == 'released'
