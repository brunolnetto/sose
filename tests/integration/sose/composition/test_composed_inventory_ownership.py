from __future__ import annotations

import pytest

from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.examples.warehouse_management import simulation as wm
from sose.persistence.memory import MemoryPersistence


def _entities_of_type(store: MemoryPersistence, entity_type: str):
    return tuple(entity for entity in store.entities() if entity.entity_type == entity_type)


def test_warehouse_management_external_reservation_and_consumption_are_idempotent() -> None:
    store = MemoryPersistence()
    entities = wm.seed_reference(
        store,
        origin_on_hand=20.0,
        transfer_quantity=1.0,
        sku=fulfillment.PRIMARY_SKU,
    )
    _, engine = wm.build_runtime(store)

    assert wm.reserve_external_stock(
        store,
        engine,
        stock_id=entities.origin_stock_id,
        quantity=10.0,
        sku=fulfillment.PRIMARY_SKU,
        reservation_reference="reservation-1",
        correlation_id="customer-flow",
    ) is True
    assert wm.reserve_external_stock(
        store,
        engine,
        stock_id=entities.origin_stock_id,
        quantity=10.0,
        sku=fulfillment.PRIMARY_SKU,
        reservation_reference="reservation-1",
        correlation_id="customer-flow",
    ) is False

    stock = store.entity("warehouse_management_stock", entities.origin_stock_id)
    assert stock.attributes["on_hand"] == 20.0
    assert stock.attributes["reserved"] == 10.0
    assert stock.attributes["external_reservations"]["reservation-1"] == {
        "sku": fulfillment.PRIMARY_SKU,
        "quantity": 10.0,
        "consumed": False,
        "consumption_reference": None,
    }

    with pytest.raises(ValueError, match="reservation replay conflict"):
        wm.reserve_external_stock(
            store,
            engine,
            stock_id=entities.origin_stock_id,
            quantity=9.0,
            sku=fulfillment.PRIMARY_SKU,
            reservation_reference="reservation-1",
            correlation_id="customer-flow",
        )

    assert wm.consume_external_reservation(
        store,
        engine,
        stock_id=entities.origin_stock_id,
        quantity=10.0,
        sku=fulfillment.PRIMARY_SKU,
        reservation_reference="reservation-1",
        consumption_reference="pick-1",
        correlation_id="customer-flow",
    ) is True
    assert wm.consume_external_reservation(
        store,
        engine,
        stock_id=entities.origin_stock_id,
        quantity=10.0,
        sku=fulfillment.PRIMARY_SKU,
        reservation_reference="reservation-1",
        consumption_reference="pick-1",
        correlation_id="customer-flow",
    ) is False

    stock = store.entity("warehouse_management_stock", entities.origin_stock_id)
    assert stock.attributes["on_hand"] == 10.0
    assert stock.attributes["reserved"] == 0.0
    assert stock.attributes["external_reservations"]["reservation-1"]["consumed"] is True
    assert (
        stock.attributes["external_reservations"]["reservation-1"][
            "consumption_reference"
        ]
        == "pick-1"
    )

    event_names = [event.name for event in store.events()]
    assert event_names.count("warehouse_management.inventory_reserved") == 1
    assert event_names.count("warehouse_management.inventory_consumed") == 1


def test_external_reservation_rejects_sku_mismatch_and_insufficient_stock() -> None:
    store = MemoryPersistence()
    entities = wm.seed_reference(
        store,
        origin_on_hand=4.0,
        transfer_quantity=1.0,
        sku=fulfillment.PRIMARY_SKU,
    )
    _, engine = wm.build_runtime(store)

    with pytest.raises(ValueError, match="reservation SKU mismatch"):
        wm.reserve_external_stock(
            store,
            engine,
            stock_id=entities.origin_stock_id,
            quantity=1.0,
            sku="other-sku",
            reservation_reference="reservation-mismatch",
        )

    assert wm.reserve_external_stock(
        store,
        engine,
        stock_id=entities.origin_stock_id,
        quantity=5.0,
        sku=fulfillment.PRIMARY_SKU,
        reservation_reference="reservation-too-large",
    ) is False

    stock = store.entity("warehouse_management_stock", entities.origin_stock_id)
    assert stock.attributes["on_hand"] == 4.0
    assert stock.attributes["reserved"] == 0.0


def test_composed_fulfillment_uses_foreign_reservation_without_local_inventory() -> None:
    store = MemoryPersistence()
    entities = fulfillment.seed_composed_reference(
        store,
        requested_quantity=10.0,
    )

    assert entities.lot_ids == ()
    assert _entities_of_type(store, "warehouse_inventory_lot") == ()

    _, engine = fulfillment.build_runtime(store)
    assert fulfillment.allocate_composed_order(
        store,
        engine,
        entities=entities,
        stock_reference="wm-stock-1",
        reservation_reference="wm-reservation-1",
        supplied_sku=fulfillment.PRIMARY_SKU,
        quantity=10.0,
    )
    assert fulfillment.pick_composed_order(
        store,
        engine,
        entities=entities,
    )
    assert fulfillment.pack_order(store, engine, entities=entities)
    assert fulfillment.ship_order(store, engine, entities=entities)

    order = store.entity("warehouse_fulfillment_order", entities.order_id)
    assert order.state == "shipped"
    allocations = [
        store.entity("warehouse_allocation", allocation_id)
        for allocation_id in order.attributes["allocation_ids"]
    ]
    assert len(allocations) == 1
    allocation = allocations[0]
    assert allocation.attributes["inventory_owner"] == "warehouse_management"
    assert allocation.attributes["stock_reference"] == "wm-stock-1"
    assert allocation.attributes["reservation_reference"] == "wm-reservation-1"
    assert "lot_id" not in allocation.attributes
    assert _entities_of_type(store, "warehouse_inventory_lot") == ()


def test_standalone_fulfillment_retains_local_inventory_lots() -> None:
    store = MemoryPersistence()
    entities = fulfillment.seed_reference(store, requested_quantity=10.0)

    assert len(entities.lot_ids) == 2
    assert len(_entities_of_type(store, "warehouse_inventory_lot")) == 2


def test_external_reservation_normalizes_quantity_before_persist_and_consume() -> None:
    store = MemoryPersistence()
    entities = wm.seed_reference(
        store,
        origin_on_hand=4.0,
        transfer_quantity=1.0,
        sku=fulfillment.PRIMARY_SKU,
    )
    _, engine = wm.build_runtime(store)

    raw_quantity = 1.0000004
    assert wm.reserve_external_stock(
        store,
        engine,
        stock_id=entities.origin_stock_id,
        quantity=raw_quantity,
        sku=fulfillment.PRIMARY_SKU,
        reservation_reference="precision-reservation",
    )

    stock = store.entity("warehouse_management_stock", entities.origin_stock_id)
    record = stock.attributes["external_reservations"]["precision-reservation"]
    assert record["quantity"] == 1.0
    assert stock.attributes["reserved"] == 1.0

    assert wm.consume_external_reservation(
        store,
        engine,
        stock_id=entities.origin_stock_id,
        quantity=raw_quantity,
        sku=fulfillment.PRIMARY_SKU,
        reservation_reference="precision-reservation",
        consumption_reference="precision-consumption",
    )
    stock = store.entity("warehouse_management_stock", entities.origin_stock_id)
    assert stock.attributes["on_hand"] == 3.0
    assert stock.attributes["reserved"] == 0.0
