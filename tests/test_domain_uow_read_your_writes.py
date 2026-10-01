from sose.domain.entity import Entity
from sose.domain.storage import DomainPersistence
from sose.domain.warehouse import MemoryDomainWarehouse
from sose.persistence.memory import MemoryPersistence


def test_multiple_entity_versions_are_read_your_writes_in_one_uow():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    domain = DomainPersistence(engine, warehouse)

    with domain.transaction() as uow:
        v0 = Entity(id="job-1", entity_type="job", state="queued", version=0)
        uow.save_entity(v0)
        assert uow.get_entity("job", "job-1") == v0

        v1 = Entity(id="job-1", entity_type="job", state="running", version=1)
        uow.save_entity(v1)
        assert uow.get_entity("job", "job-1") == v1

        v2 = Entity(id="job-1", entity_type="job", state="closed", version=2)
        uow.save_entity(v2)
        assert uow.get_entity("job", "job-1") == v2

    assert engine.entity("job", "job-1") is None
    assert [d.mutation.entity.version for d in engine.domain_deliveries()] == [0, 1, 2]
