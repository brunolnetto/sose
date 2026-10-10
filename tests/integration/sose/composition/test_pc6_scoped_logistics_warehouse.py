"""Logistics attempts and warehouse facilities must not alias across instances."""
from sose.examples.logistics import simulation as logistics
from sose.examples.warehouse_management import simulation as warehouse
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
import pytest


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_two_isolated_logistics_and_warehouse_instances_survive_restart(adapter, tmp_path):
    path = tmp_path / "pc6-wm-logistics.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLiteIncrementalPersistence(path)
    try:
        refs = {}
        for key in ("org-a", "org-b"):
            wm_ref = warehouse.seed_reference(store, instance_key=key, sku="SKU-A")
            ship_ref = logistics.seed_reference(store, instance_key=key)
            _, engine = warehouse.build_runtime(store)
            assert warehouse.reserve_external_stock(
                store, engine, stock_id=wm_ref.origin_stock_id, quantity=3.0,
                sku="SKU-A", reservation_reference=f"reservation-{key}",
                correlation_id=f"pc6-{key}",
            )
            _, logistics_engine = logistics.build_runtime(store)
            attempt = logistics.ensure_delivery_attempt(
                store, logistics_engine, ordinal=1, shipment_id=ship_ref.shipment_id,
            )
            assert attempt.attributes["shipment_id"] == ship_ref.shipment_id
            assert attempt.id == logistics.delivery_attempt_id(
                1, shipment_id=ship_ref.shipment_id,
            )
            refs[key] = (wm_ref, ship_ref, attempt.id)
        a, b = refs.values()
        assert a[0].origin_stock_id != b[0].origin_stock_id
        assert a[0].destination_site_id != b[0].destination_site_id
        assert a[1].shipment_id != b[1].shipment_id
        assert a[2] != b[2]
        if adapter == "sqlite":
            store.close()
            store = SQLiteIncrementalPersistence(path)
        for key, (warehouse_ref, shipment_ref, attempt_id) in refs.items():
            stock = store.entity("warehouse_management_stock", warehouse_ref.origin_stock_id)
            assert stock.attributes["reserved"] == 3.0
            assert set(stock.attributes["external_reservations"]) == {f"reservation-{key}"}
            assert store.entity("shipment", shipment_ref.shipment_id) is not None
            assert store.entity("delivery_attempt", attempt_id).attributes["shipment_id"] == shipment_ref.shipment_id
        assert len([x for x in store.events() if x.name == "warehouse_management.inventory_reserved"]) == 2
    finally:
        if adapter == "sqlite":
            store.close()


def test_legacy_logistics_and_warehouse_default_ids_unchanged():
    from sose.core.identity import deterministic_id
    store = MemoryPersistence()
    ship = logistics.seed_reference(store)
    site = warehouse.seed_reference(store)
    assert ship.shipment_id == deterministic_id("entity", "shipment", "logistics-reference", "shipment-1")
    assert logistics.delivery_attempt_id(1) == deterministic_id(
        "entity", "delivery_attempt", "logistics-reference", "shipment-1", "attempt", 1,
    )
    assert site.origin_site_id == deterministic_id("entity", "warehouse_management_site", "warehouse-management", "site-origin")


@pytest.mark.parametrize("seed", [logistics.seed_reference, warehouse.seed_reference])
def test_empty_warehouse_logistics_instance_key_is_rejected(seed):
    with pytest.raises(ValueError, match="instance_key"):
        seed(MemoryPersistence(), instance_key="")
