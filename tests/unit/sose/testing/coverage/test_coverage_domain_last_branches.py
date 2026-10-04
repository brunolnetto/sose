from sose.domain.delivery import DomainDelivery
from sose.domain.entity import Entity
from sose.domain.sqlite import SQLiteDomainWarehouse
from sose.domain.storage import DomainPersistence
from sose.domain.warehouse import DomainMutation, MemoryDomainWarehouse
from sose.persistence.memory import MemoryPersistence

import pytest


def test_sqlite_domain_memory_path_reaches_wal_failure_contract():
    with pytest.raises(RuntimeError, match="requires WAL journal mode"):
        SQLiteDomainWarehouse(":memory:")


def test_domain_entities_continues_across_multiple_non_conflicting_deliveries():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    persistence = DomainPersistence(engine, warehouse)

    warehouse.apply(
        DomainMutation(
            "warehouse-order-v1",
            Entity(id="1", entity_type="order", state="queued", version=1),
        )
    )
    deliveries = (
        DomainDelivery(
            DomainMutation(
                "pending-order-v2",
                Entity(id="1", entity_type="order", state="running", version=2),
            )
        ),
        DomainDelivery(
            DomainMutation(
                "pending-invoice-v1",
                Entity(id="2", entity_type="invoice", state="open", version=1),
            )
        ),
    )
    with engine.transaction() as uow:
        for delivery in deliveries:
            uow.save_domain_delivery(delivery)

    assert persistence.entities() == (
        Entity(id="1", entity_type="order", state="running", version=2),
        Entity(id="2", entity_type="invoice", state="open", version=1),
    )


def test_sqlite_domain_close_tolerates_missing_connection_reference():
    warehouse = object.__new__(SQLiteDomainWarehouse)
    warehouse._closed = False
    warehouse._connection = None

    SQLiteDomainWarehouse.close(warehouse)

    assert warehouse._closed is True


def test_domain_entities_rejects_conflicting_equal_version_from_delivery():
    current = Entity(id="1", entity_type="order", state="queued", version=3)
    conflicting = Entity(id="1", entity_type="order", state="running", version=3)
    delivery = DomainDelivery(
        DomainMutation("pending-order-v3", conflicting)
    )

    engine = MemoryPersistence()
    with engine.transaction() as uow:
        uow.save_domain_delivery(delivery)

    warehouse = MemoryDomainWarehouse()
    warehouse.apply(DomainMutation("warehouse-order-v3", current))
    persistence = DomainPersistence(engine, warehouse)

    with pytest.raises(RuntimeError, match="conflicting domain entity version"):
        persistence.entities()


def test_domain_entities_accepts_identical_equal_version_from_delivery():
    current = Entity(id="1", entity_type="order", state="queued", version=3)
    delivery = DomainDelivery(
        DomainMutation("pending-order-v3", current)
    )

    engine = MemoryPersistence()
    with engine.transaction() as uow:
        uow.save_domain_delivery(delivery)

    warehouse = MemoryDomainWarehouse()
    warehouse.apply(DomainMutation("warehouse-order-v3", current))
    persistence = DomainPersistence(engine, warehouse)

    assert persistence.entities() == (current,)
