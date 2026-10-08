from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.warehouse_fulfillment.config import WarehouseFulfillmentConfig
from sose.examples.warehouse_fulfillment.scenarios import ORIGIN
from sose.examples.warehouse_fulfillment.simulation import (
    allocate_order,
    build_runtime,
    pack_order,
    pick_order,
    seed_reference,
    service_task_id,
    ship_order,
)
from sose.persistence.memory import MemoryPersistence


PICK = timedelta(hours=2)
PACK = timedelta(hours=1)
SHIP = timedelta(hours=3)


def _runtime(*, picker_capacity: int = 1):
    persistence = MemoryPersistence()
    entities = seed_reference(
        persistence,
        picker_capacity=picker_capacity,
        packing_station_capacity=1,
        shipping_dock_capacity=1,
    )
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    assert allocate_order(persistence, engine, entities=entities)
    return persistence, entities, engine, backend


def _next_due(persistence):
    return min(work.due_at for work in persistence.scheduled_work())


def test_warehouse_config_requires_positive_service_capacity_and_duration() -> None:
    config = WarehouseFulfillmentConfig()
    assert config.picker_capacity == 1
    assert config.packing_station_capacity == 1
    assert config.shipping_dock_capacity == 1
    assert config.pick_duration > timedelta(0)
    assert config.pack_duration > timedelta(0)
    assert config.ship_duration > timedelta(0)

    with pytest.raises(ValueError):
        WarehouseFulfillmentConfig(picker_capacity=0)
    with pytest.raises(ValueError):
        WarehouseFulfillmentConfig(pick_duration=timedelta(0))


def test_picker_capacity_creates_real_contention_without_early_inventory_effect() -> None:
    persistence, entities, engine, backend = _runtime(picker_capacity=1)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    allocation_ids = tuple(str(value) for value in order.attributes["allocation_ids"])
    assert len(allocation_ids) == 2

    primary_before = persistence.entity("warehouse_inventory_lot", entities.lot_ids[0])
    substitute_before = persistence.entity("warehouse_inventory_lot", entities.lot_ids[1])
    assert primary_before is not None and substitute_before is not None

    assert pick_order(
        persistence,
        engine,
        backend,
        entities=entities,
        duration=PICK,
    ) is False

    tasks = tuple(
        persistence.entity(
            "warehouse_fulfillment_service_task",
            service_task_id("pick", allocation_id),
        )
        for allocation_id in allocation_ids
    )
    assert all(task is not None for task in tasks)
    assert sorted(task.state for task in tasks if task is not None) == [
        "in_progress",
        "queued",
    ]

    picker_reservations = tuple(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.resource_name == "fulfillment_picker"
    )
    picker_demands = tuple(
        demand
        for demand in persistence.resource_demands()
        if demand.resource_name == "fulfillment_picker"
    )
    assert len(picker_reservations) == 1
    assert len(picker_demands) == 1
    assert len(persistence.scheduled_work()) == 1

    primary = persistence.entity("warehouse_inventory_lot", entities.lot_ids[0])
    substitute = persistence.entity("warehouse_inventory_lot", entities.lot_ids[1])
    assert primary is not None and substitute is not None
    assert primary.attributes["on_hand"] == primary_before.attributes["on_hand"]
    assert substitute.attributes["on_hand"] == substitute_before.attributes["on_hand"]

    backend.run_until(_next_due(persistence))

    completed = [
        persistence.entity(
            "warehouse_fulfillment_service_task",
            service_task_id("pick", allocation_id),
        )
        for allocation_id in allocation_ids
    ]
    assert sum(task is not None and task.state == "completed" for task in completed) == 1
    assert all(
        persistence.entity("warehouse_allocation", allocation_id).state == "committed"
        for allocation_id in allocation_ids
    )

    assert pick_order(
        persistence,
        engine,
        backend,
        entities=entities,
        duration=PICK,
    ) is False

    assert sum(
        persistence.entity("warehouse_allocation", allocation_id).state == "picked"
        for allocation_id in allocation_ids
    ) == 1
    assert len(
        tuple(
            reservation
            for reservation in persistence.resource_reservations()
            if reservation.resource_name == "fulfillment_picker"
        )
    ) == 1
    assert len(persistence.scheduled_work()) == 1

    backend.run_until(_next_due(persistence))
    assert pick_order(
        persistence,
        engine,
        backend,
        entities=entities,
        duration=PICK,
    ) is True
    assert all(
        persistence.entity("warehouse_allocation", allocation_id).state == "picked"
        for allocation_id in allocation_ids
    )
    assert persistence.resource_reservations() == ()


def test_pack_and_ship_have_explicit_service_time_before_business_transition() -> None:
    persistence, entities, engine, backend = _runtime()

    while not pick_order(
        persistence,
        engine,
        backend,
        entities=entities,
        duration=PICK,
    ):
        backend.run_until(_next_due(persistence))

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "picking"

    assert pack_order(
        persistence,
        engine,
        backend,
        entities=entities,
        duration=PACK,
    ) is False
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "picking"

    backend.run_until(_next_due(persistence))
    task = persistence.entity(
        "warehouse_fulfillment_service_task",
        service_task_id("pack", entities.order_id),
    )
    assert task is not None and task.state == "completed"
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "picking"

    assert pack_order(
        persistence,
        engine,
        backend,
        entities=entities,
        duration=PACK,
    ) is True
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "packed"

    assert ship_order(
        persistence,
        engine,
        backend,
        entities=entities,
        duration=SHIP,
    ) is False
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "packed"

    backend.run_until(_next_due(persistence))
    assert ship_order(
        persistence,
        engine,
        backend,
        entities=entities,
        duration=SHIP,
    ) is True

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "shipped"
    assert persistence.resource_reservations() == ()
    assert persistence.resource_demands() == ()
    assert persistence.scheduled_work() == ()


def test_service_task_persists_stage_timing_evidence() -> None:
    persistence, entities, engine, backend = _runtime()

    assert pick_order(
        persistence,
        engine,
        backend,
        entities=entities,
        duration=PICK,
    ) is False

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    first_allocation = str(order.attributes["allocation_ids"][0])
    task = persistence.entity(
        "warehouse_fulfillment_service_task",
        service_task_id("pick", first_allocation),
    )
    assert task is not None and task.state == "in_progress"
    assert task.attributes["requested_at"] == ORIGIN.isoformat()
    assert task.attributes["acquired_at"] == ORIGIN.isoformat()
    assert task.attributes["service_duration_seconds"] == PICK.total_seconds()
    assert task.attributes["completion_due_at"] == (ORIGIN + PICK).isoformat()
