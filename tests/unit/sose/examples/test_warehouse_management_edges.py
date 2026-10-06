from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from sose.examples.warehouse_management.simulation import (
    ORIGIN,
    arrive_truck,
    assign_forklift,
    build_runtime,
    complete_unload,
    seed_reference,
    shipment_kpis,
    start_transfer,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(**seed_kwargs):
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, now=ORIGIN, **seed_kwargs)
    _, engine = build_runtime(persistence, now=ORIGIN)
    return persistence, engine, entities


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"origin_on_hand": -1.0}, "origin_on_hand"),
        ({"transfer_quantity": 0.0}, "transfer_quantity"),
        ({"dock_count": 0}, "dock_count"),
        ({"forklift_count": 0}, "forklift_count"),
        ({"planned_completion_hours": 0.0}, "planned_completion_hours"),
    ],
)
def test_seed_rejects_invalid_reference_parameters(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        seed_reference(MemoryPersistence(), **kwargs)


def test_missing_persisted_entity_is_reported_at_operation_boundary() -> None:
    persistence, engine, entities = _runtime()
    missing = replace(entities, shipment_id="missing-shipment")

    with pytest.raises(RuntimeError, match="was not persisted"):
        start_transfer(persistence, engine, entities=missing)


def test_started_transfer_is_idempotent_and_does_not_reserve_twice() -> None:
    persistence, engine, entities = _runtime(origin_on_hand=20.0, transfer_quantity=8.0)

    assert start_transfer(persistence, engine, entities=entities) is True
    assert start_transfer(persistence, engine, entities=entities) is True

    stock = persistence.entity("warehouse_management_stock", entities.origin_stock_id)
    assert stock is not None
    assert stock.attributes["reserved"] == pytest.approx(8.0)


def test_in_transit_without_durable_reservation_is_rejected() -> None:
    persistence, engine, entities = _runtime()
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment is not None
    shipment.state = "in_transit"
    shipment.attributes["stock_reserved"] = False
    _save(persistence, shipment)

    with pytest.raises(RuntimeError, match="missing durable stock reservation"):
        start_transfer(persistence, engine, entities=entities)


def test_delayed_arrival_recovers_when_dock_becomes_available() -> None:
    persistence, engine, entities = _runtime(dock_count=1)
    dock = persistence.entity("warehouse_management_dock", entities.dock_ids[0])
    assert dock is not None
    dock.state = "occupied"
    _save(persistence, dock)

    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=1)
    assert arrive_truck(persistence, engine, entities=entities) is False
    assert arrive_truck(persistence, engine, entities=entities) is False

    dock = persistence.entity("warehouse_management_dock", entities.dock_ids[0])
    assert dock is not None
    dock.state = "available"
    _save(persistence, dock)

    assert arrive_truck(persistence, engine, entities=entities) is True
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    assert shipment is not None and shipment.state == "docked"
    assert truck is not None and truck.state == "docked"


def test_available_capacity_search_skips_busy_first_resource() -> None:
    persistence, engine, entities = _runtime(dock_count=2, forklift_count=2)
    first_dock = persistence.entity("warehouse_management_dock", entities.dock_ids[0])
    first_forklift = persistence.entity("warehouse_management_forklift", entities.forklift_ids[0])
    assert first_dock is not None and first_forklift is not None
    first_dock.state = "occupied"
    first_forklift.state = "assigned"
    _save(persistence, first_dock)
    _save(persistence, first_forklift)

    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=1)
    assert arrive_truck(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is True

    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment is not None
    assert shipment.attributes["assigned_dock_id"] == entities.dock_ids[1]
    assert shipment.attributes["assigned_forklift_id"] == entities.forklift_ids[1]


def test_assign_forklift_is_idempotent_after_handling_starts() -> None:
    persistence, engine, entities = _runtime()
    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=1)
    assert arrive_truck(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is True


def test_assign_forklift_rejects_non_docked_shipment() -> None:
    persistence, engine, entities = _runtime()
    assert assign_forklift(persistence, engine, entities=entities) is False


def test_completion_rejects_missing_handling_ownership() -> None:
    persistence, engine, entities = _runtime()
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment is not None
    shipment.state = "handling"
    shipment.attributes["assigned_dock_id"] = None
    shipment.attributes["assigned_forklift_id"] = None
    _save(persistence, shipment)

    with pytest.raises(RuntimeError, match="missing dock or forklift ownership"):
        complete_unload(persistence, engine, entities=entities)


def test_completion_rejects_inconsistent_reserved_stock() -> None:
    persistence, engine, entities = _runtime()
    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=1)
    assert arrive_truck(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is True

    stock = persistence.entity("warehouse_management_stock", entities.origin_stock_id)
    assert stock is not None
    stock.attributes["reserved"] = 0.0
    _save(persistence, stock)

    with pytest.raises(RuntimeError, match="reserved transfer stock is inconsistent"):
        complete_unload(persistence, engine, entities=entities)


def test_completed_transfer_and_kpi_projection_are_idempotent() -> None:
    persistence, engine, entities = _runtime()
    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=1)
    assert arrive_truck(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=2)
    assert complete_unload(persistence, engine, entities=entities) is True
    assert complete_unload(persistence, engine, entities=entities) is True
    assert arrive_truck(persistence, engine, entities=entities) is True

    kpis = shipment_kpis(persistence, entities=entities)
    assert kpis["completed"] is True
    assert kpis["lateness_seconds"] == pytest.approx(0.0)
