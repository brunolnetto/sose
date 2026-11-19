from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.warehouse_fulfillment.config import WarehouseFulfillmentConfig
from sose.examples.warehouse_fulfillment.simulation import (
    ORIGIN,
    allocate_order,
    build_runtime,
    reconcile_fulfillment_services,
    seed_reference,
)
from sose.examples.warehouse_fulfillment.records import service_task_id
from sose.persistence.memory import MemoryPersistence


def _runtime(*, picker_capacity: int = 1):
    persistence = MemoryPersistence()
    entities = seed_reference(
        persistence,
        owner_id="warehouse-fulfillment-capacity-reference",
        picker_capacity=picker_capacity,
        packing_station_capacity=1,
        shipping_dock_capacity=1,
    )
    _, engine = build_runtime(persistence, owner_id="warehouse-fulfillment-capacity-reference")
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    assert allocate_order(persistence, engine, entities=entities)
    return persistence, entities, engine, backend


def _reconcile(persistence, entities, engine, backend):
    reconcile_fulfillment_services(
        persistence,
        engine,
        backend,
        entities=entities,
        pick_duration=timedelta(hours=1),
        pack_duration=timedelta(hours=1),
        ship_duration=timedelta(hours=1),
    )


def test_config_exposes_positive_service_capacity_and_duration() -> None:
    config = WarehouseFulfillmentConfig()

    assert config.picker_capacity >= 1
    assert config.packing_station_capacity >= 1
    assert config.shipping_dock_capacity >= 1
    assert config.pick_duration > timedelta(0)
    assert config.pack_duration > timedelta(0)
    assert config.ship_duration > timedelta(0)

    with pytest.raises(ValueError):
        WarehouseFulfillmentConfig(picker_capacity=0)
    with pytest.raises(ValueError):
        WarehouseFulfillmentConfig(pick_duration=timedelta(0))


def test_seed_defines_service_resources_without_using_inventory_as_capacity() -> None:
    persistence, _, _, _ = _runtime(picker_capacity=2)

    definitions = {
        definition.name: definition.capacity
        for definition in persistence.resource_definitions()
    }
    assert definitions == {
        "fulfillment_picker": 2,
        "packing_station": 1,
        "shipping_dock": 1,
    }


def test_picker_contention_queues_second_allocation_without_early_inventory_effect() -> None:
    persistence, entities, engine, backend = _runtime(picker_capacity=1)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    allocation_ids = tuple(str(value) for value in order.attributes["allocation_ids"])

    _reconcile(persistence, entities, engine, backend)

    tasks = [
        persistence.entity(
            "warehouse_fulfillment_service_task",
            service_task_id("pick", allocation_id),
        )
        for allocation_id in allocation_ids
    ]
    assert all(task is not None for task in tasks)
    assert [task.state for task in tasks] == ["in_progress", "queued"]
    assert len(persistence.resource_reservations()) == 1
    assert len(persistence.resource_demands()) == 1

    primary = persistence.entity("warehouse_inventory_lot", entities.lot_ids[0])
    substitute = persistence.entity("warehouse_inventory_lot", entities.lot_ids[1])
    assert primary is not None and primary.attributes["on_hand"] == pytest.approx(6.0)
    assert substitute is not None and substitute.attributes["on_hand"] == pytest.approx(5.0)


def test_service_duration_and_queue_wait_gate_business_completion() -> None:
    persistence, entities, engine, backend = _runtime(picker_capacity=1)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    first_id, second_id = tuple(str(value) for value in order.attributes["allocation_ids"])

    _reconcile(persistence, entities, engine, backend)
    backend.run_until(ORIGIN + timedelta(hours=1))
    _reconcile(persistence, entities, engine, backend)

    first = persistence.entity("warehouse_allocation", first_id)
    second = persistence.entity("warehouse_allocation", second_id)
    second_task = persistence.entity(
        "warehouse_fulfillment_service_task",
        service_task_id("pick", second_id),
    )
    assert first is not None and first.state == "picked"
    assert second is not None and second.state == "committed"
    assert second_task is not None and second_task.state == "in_progress"
    assert second_task.attributes["requested_at"] == ORIGIN.isoformat()
    assert second_task.attributes["acquired_at"] == (
        ORIGIN + timedelta(hours=1)
    ).isoformat()

    backend.run_until(ORIGIN + timedelta(hours=2))
    _reconcile(persistence, entities, engine, backend)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "picking"

    backend.run_until(ORIGIN + timedelta(hours=3))
    _reconcile(persistence, entities, engine, backend)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "packed"

    backend.run_until(ORIGIN + timedelta(hours=4))
    _reconcile(persistence, entities, engine, backend)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "shipped"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()

    primary = persistence.entity("warehouse_inventory_lot", entities.lot_ids[0])
    substitute = persistence.entity("warehouse_inventory_lot", entities.lot_ids[1])
    assert primary is not None and primary.attributes["on_hand"] == pytest.approx(0.0)
    assert substitute is not None and substitute.attributes["on_hand"] == pytest.approx(1.0)



def test_two_pickers_execute_in_parallel_without_early_inventory_consumption() -> None:
    """W1 missing falsifier: capacity=2 is actually honored, not capped to one.

    The existing capacity=1 case proves a second demand queues. This case
    catches a broken implementation that silently forces every picker pool
    to capacity=1, and verifies both service grants, due times, stock effects
    and eventual finite-capacity release.
    """
    persistence, entities, engine, backend = _runtime(picker_capacity=2)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    allocation_ids = tuple(str(v) for v in order.attributes["allocation_ids"])
    assert len(allocation_ids) == 2

    _reconcile(persistence, entities, engine, backend)
    tasks = [
        persistence.entity(
            "warehouse_fulfillment_service_task",
            service_task_id("pick", allocation_id),
        )
        for allocation_id in allocation_ids
    ]
    assert all(task is not None for task in tasks)
    assert [task.state for task in tasks] == ["in_progress", "in_progress"]
    assert all(task.attributes["acquired_at"] == ORIGIN.isoformat() for task in tasks)
    assert all(
        task.attributes["completion_due_at"]
        == (ORIGIN + timedelta(hours=1)).isoformat()
        for task in tasks
    )
    reservations = persistence.resource_reservations()
    assert len(reservations) == 2
    assert len({reservation.reservation_id for reservation in reservations}) == 2
    assert {
        task.attributes["reservation_id"] for task in tasks
    } == {reservation.reservation_id for reservation in reservations}
    assert persistence.resource_demands() == ()

    for lot_id, expected in zip(entities.lot_ids, (6.0, 5.0), strict=True):
        lot = persistence.entity("warehouse_inventory_lot", lot_id)
        assert lot is not None and lot.attributes["on_hand"] == pytest.approx(expected)

    # A repeated reconcile cannot create ghost reservations/duplicate work.
    _reconcile(persistence, entities, engine, backend)
    assert {r.reservation_id for r in persistence.resource_reservations()} == {
        r.reservation_id for r in reservations
    }
    assert persistence.resource_demands() == ()

    backend.run_until(ORIGIN + timedelta(hours=1))
    _reconcile(persistence, entities, engine, backend)
    allocations = [
        persistence.entity("warehouse_allocation", allocation_id)
        for allocation_id in allocation_ids
    ]
    assert all(a is not None and a.state == "picked" for a in allocations)
    # Picking completed at t=1, but packing/shipping need their own duration.
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "picking"

    backend.run_until(ORIGIN + timedelta(hours=2))
    _reconcile(persistence, entities, engine, backend)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "packed"

    backend.run_until(ORIGIN + timedelta(hours=3))
    _reconcile(persistence, entities, engine, backend)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "shipped"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
