import pytest

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
    assert sorted(d.mutation.entity.version for d in engine.domain_deliveries()) == [0, 1, 2]


def test_domain_delivery_rolls_back_with_caller_uow():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    domain = DomainPersistence(engine, warehouse)

    with pytest.raises(RuntimeError, match="abort"):
        with domain.transaction() as uow:
            uow.save_entity(Entity(id="job-1", entity_type="job", state="queued", version=0))
            raise RuntimeError("abort")

    assert engine.domain_deliveries() == ()
    assert engine.entity("job", "job-1") is None
    assert warehouse.entity("job", "job-1") is None


def test_same_version_content_change_gets_monotonic_persistence_revision():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    domain = DomainPersistence(engine, warehouse)
    seed = Entity(id="job-1", entity_type="job", state="queued", version=0)
    warehouse.apply(DomainMutation("seed", seed))

    with domain.transaction() as uow:
        changed = uow.get_entity("job", "job-1")
        assert changed is not None
        changed.attributes["owner"] = "worker-1"
        uow.save_entity(changed)
        persisted = uow.get_entity("job", "job-1")
        assert persisted is not None
        assert persisted.version == 1
        assert persisted.attributes["owner"] == "worker-1"
