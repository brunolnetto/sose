from datetime import datetime, timezone

import pytest

from sose.domain.entity import Entity
from sose.domain.warehouse import (
    DomainApplyResult,
    DomainMutation,
    MemoryDomainWarehouse,
)


def entity(version: int, state: str) -> Entity:
    return Entity(
        id="order-1",
        entity_type="order",
        state=state,
        version=version,
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_warehouse_never_regresses_entity_version():
    warehouse = MemoryDomainWarehouse()
    assert warehouse.apply(DomainMutation("v2", entity(2, "running"))) is DomainApplyResult.APPLIED
    assert warehouse.apply(DomainMutation("v1", entity(1, "queued"))) is DomainApplyResult.SUPERSEDED
    assert warehouse.entity("order", "order-1") == entity(2, "running")


def test_same_version_identical_state_is_replay():
    warehouse = MemoryDomainWarehouse()
    current = entity(2, "running")
    warehouse.apply(DomainMutation("first-v2", current))
    assert warehouse.apply(DomainMutation("second-v2", current)) is DomainApplyResult.REPLAYED


def test_same_version_different_state_is_conflict():
    warehouse = MemoryDomainWarehouse()
    warehouse.apply(DomainMutation("v2-running", entity(2, "running")))
    with pytest.raises(ValueError, match="conflicting domain entity version"):
        warehouse.apply(DomainMutation("v2-closed", entity(2, "closed")))
