from __future__ import annotations

from datetime import timedelta

from sose.examples.warehouse_management.simulation import (
    ORIGIN,
    arrive_truck,
    assign_forklift,
    build_runtime,
    complete_unload,
    seed_reference,
    start_transfer,
)
from sose.persistence.memory import MemoryPersistence


def _engine(store: MemoryPersistence, *, now):
    _, engine = build_runtime(store, now=now)
    return engine


def _snapshot(store: MemoryPersistence, ids):
    return {
        "origin_site": store.entity("warehouse_management_site", ids.origin_site_id),
        "destination_site": store.entity(
            "warehouse_management_site", ids.destination_site_id
        ),
        "docks": tuple(
            store.entity("warehouse_management_dock", dock_id)
            for dock_id in ids.dock_ids
        ),
        "forklifts": tuple(
            store.entity("warehouse_management_forklift", forklift_id)
            for forklift_id in ids.forklift_ids
        ),
        "truck": store.entity("warehouse_management_truck", ids.truck_id),
        "origin_stock": store.entity(
            "warehouse_management_stock", ids.origin_stock_id
        ),
        "destination_stock": store.entity(
            "warehouse_management_stock", ids.destination_stock_id
        ),
        "shipment": store.entity(
            "warehouse_management_shipment", ids.shipment_id
        ),
        "events": store.events(),
        "scheduled_work": store.scheduled_work(),
        "position": store.simulation_position(),
    }


def _run_continuous():
    store = MemoryPersistence()
    ids = seed_reference(store, now=ORIGIN, origin_on_hand=20.0, transfer_quantity=8.0)
    engine = _engine(store, now=ORIGIN)

    assert start_transfer(store, engine, entities=ids) is True

    engine.context.clock.now = ORIGIN + timedelta(hours=2)
    assert arrive_truck(store, engine, entities=ids) is True
    assert assign_forklift(store, engine, entities=ids) is True

    engine.context.clock.now = ORIGIN + timedelta(hours=3)
    assert complete_unload(store, engine, entities=ids) is True
    return store, ids


def _run_with_restarts():
    store = MemoryPersistence()
    ids = seed_reference(store, now=ORIGIN, origin_on_hand=20.0, transfer_quantity=8.0)

    engine1 = _engine(store, now=ORIGIN)
    assert start_transfer(store, engine1, entities=ids) is True

    engine2 = _engine(store, now=ORIGIN + timedelta(hours=2))
    assert arrive_truck(store, engine2, entities=ids) is True

    engine3 = _engine(store, now=ORIGIN + timedelta(hours=2))
    assert assign_forklift(store, engine3, entities=ids) is True

    engine4 = _engine(store, now=ORIGIN + timedelta(hours=3))
    assert complete_unload(store, engine4, entities=ids) is True
    return store, ids


def test_warehouse_management_restart_equivalence_is_a_pc4_gate() -> None:
    continuous_store, continuous_ids = _run_continuous()
    restarted_store, restarted_ids = _run_with_restarts()

    assert _snapshot(restarted_store, restarted_ids) == _snapshot(
        continuous_store,
        continuous_ids,
    )

    shipment = restarted_store.entity(
        "warehouse_management_shipment",
        restarted_ids.shipment_id,
    )
    origin_stock = restarted_store.entity(
        "warehouse_management_stock",
        restarted_ids.origin_stock_id,
    )
    destination_stock = restarted_store.entity(
        "warehouse_management_stock",
        restarted_ids.destination_stock_id,
    )

    assert shipment is not None and shipment.state == "completed"
    assert shipment.attributes["resources_released"] is True
    assert origin_stock is not None and origin_stock.attributes["on_hand"] == 12.0
    assert origin_stock.attributes["reserved"] == 0.0
    assert destination_stock is not None
    assert destination_stock.attributes["on_hand"] == 8.0
