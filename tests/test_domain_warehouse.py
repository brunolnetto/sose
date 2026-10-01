import pytest

from sose.domain.entity import Entity
from sose.domain.warehouse import DomainApplyResult, DomainMutation, MemoryDomainWarehouse


def entity(state: str = "planned") -> Entity:
    return Entity(id="wo-1", entity_type="work_order", state=state)


def test_domain_warehouse_owns_business_state_independently():
    warehouse = MemoryDomainWarehouse()
    assert warehouse.apply(DomainMutation("seed:wo-1", entity())) is DomainApplyResult.APPLIED
    assert warehouse.entity("work_order", "wo-1") == entity()
    assert warehouse.entities("work_order") == (entity(),)


def test_domain_mutation_replay_is_idempotent():
    warehouse = MemoryDomainWarehouse()
    mutation = DomainMutation("transition:wo-1:release", entity("released"))
    assert warehouse.apply(mutation) is DomainApplyResult.APPLIED
    assert warehouse.apply(mutation) is DomainApplyResult.REPLAYED
    assert warehouse.entity("work_order", "wo-1").state == "released"


def test_domain_mutation_identity_is_immutable():
    warehouse = MemoryDomainWarehouse()
    warehouse.apply(DomainMutation("transition:wo-1:release", entity("released")))
    with pytest.raises(ValueError, match="identity conflict"):
        warehouse.apply(
            DomainMutation("transition:wo-1:release", entity("cancelled"))
        )
