from __future__ import annotations

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


def _fresh_engine(persistence, *, now=ORIGIN):
    _, engine = build_runtime(persistence, now=now)
    return engine


def test_multisite_transfer_happy_path_moves_stock_and_releases_capacity() -> None:
    persistence, engine, entities = _runtime(origin_on_hand=20.0, transfer_quantity=8.0)

    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=2)
    assert arrive_truck(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=3)
    assert complete_unload(persistence, engine, entities=entities) is True

    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    dock = persistence.entity("warehouse_management_dock", entities.dock_ids[0])
    forklift = persistence.entity("warehouse_management_forklift", entities.forklift_ids[0])
    origin_stock = persistence.entity("warehouse_management_stock", entities.origin_stock_id)
    destination_stock = persistence.entity("warehouse_management_stock", entities.destination_stock_id)

    assert shipment is not None and shipment.state == "completed"
    assert shipment.attributes["resources_released"] is True
    assert truck is not None and truck.state == "released"
    assert dock is not None and dock.state == "available"
    assert forklift is not None and forklift.state == "available"
    assert origin_stock is not None and origin_stock.attributes["on_hand"] == pytest.approx(12.0)
    assert destination_stock is not None and destination_stock.attributes["on_hand"] == pytest.approx(8.0)

    kpis = shipment_kpis(persistence, entities=entities)
    assert kpis["on_time"] is True
    assert kpis["lead_time_seconds"] == pytest.approx(3 * 3600)
    assert kpis["completed"] is True


def test_no_available_dock_keeps_truck_waiting_and_marks_shipment_delayed() -> None:
    persistence, engine, entities = _runtime(dock_count=1)
    dock = persistence.entity("warehouse_management_dock", entities.dock_ids[0])
    assert dock is not None
    dock.state = "occupied"
    with persistence.transaction() as uow:
        uow.save_entity(dock)

    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=2)
    assert arrive_truck(persistence, engine, entities=entities) is False

    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    assert shipment is not None and shipment.state == "delayed"
    assert truck is not None and truck.state == "waiting_dock"


def test_missing_forklift_blocks_handling_without_losing_dock_ownership() -> None:
    persistence, engine, entities = _runtime(forklift_count=1)
    forklift = persistence.entity("warehouse_management_forklift", entities.forklift_ids[0])
    assert forklift is not None
    forklift.state = "assigned"
    with persistence.transaction() as uow:
        uow.save_entity(forklift)

    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=2)
    assert arrive_truck(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is False

    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    dock = persistence.entity("warehouse_management_dock", entities.dock_ids[0])
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    assert shipment is not None and shipment.state == "docked"
    assert dock is not None and dock.state == "occupied"
    assert truck is not None and truck.state == "docked"


def test_insufficient_origin_stock_refuses_transfer_without_partial_side_effects() -> None:
    persistence, engine, entities = _runtime(origin_on_hand=3.0, transfer_quantity=8.0)

    assert start_transfer(persistence, engine, entities=entities) is False

    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    stock = persistence.entity("warehouse_management_stock", entities.origin_stock_id)
    assert shipment is not None and shipment.state == "planned"
    assert truck is not None and truck.state == "scheduled"
    assert stock is not None and stock.attributes["on_hand"] == pytest.approx(3.0)
    assert stock.attributes["reserved"] == pytest.approx(0.0)


def test_late_completion_is_reported_as_not_on_time() -> None:
    persistence, engine, entities = _runtime(planned_completion_hours=2.0)
    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=2)
    assert arrive_truck(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=4)
    assert complete_unload(persistence, engine, entities=entities) is True

    kpis = shipment_kpis(persistence, entities=entities)
    assert kpis["on_time"] is False
    assert kpis["lateness_seconds"] == pytest.approx(2 * 3600)


def test_start_transfer_recovers_after_reservation_commits_before_dispatch(monkeypatch) -> None:
    persistence, engine, entities = _runtime(origin_on_hand=20.0, transfer_quantity=8.0)

    def fail_dispatch(_command):
        raise RuntimeError("simulated crash after reservation")

    monkeypatch.setattr(engine, "dispatch", fail_dispatch)
    with pytest.raises(RuntimeError, match="simulated crash"):
        start_transfer(persistence, engine, entities=entities)

    stock = persistence.entity("warehouse_management_stock", entities.origin_stock_id)
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert stock is not None and stock.attributes["reserved"] == pytest.approx(8.0)
    assert shipment is not None and shipment.state == "planned"
    assert shipment.attributes["stock_reserved"] is True

    recovered = _fresh_engine(persistence)
    assert start_transfer(persistence, recovered, entities=entities) is True

    stock = persistence.entity("warehouse_management_stock", entities.origin_stock_id)
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    assert stock is not None and stock.attributes["reserved"] == pytest.approx(8.0)
    assert shipment is not None and shipment.state == "in_transit"
    assert truck is not None and truck.state == "in_transit"


def test_start_transfer_recovers_when_shipment_transition_commits_before_truck(monkeypatch) -> None:
    persistence, engine, entities = _runtime(origin_on_hand=20.0, transfer_quantity=8.0)
    original_dispatch = engine.dispatch
    dispatches = 0

    def fail_second(command):
        nonlocal dispatches
        dispatches += 1
        if dispatches == 2:
            raise RuntimeError("simulated crash before truck transition")
        original_dispatch(command)

    monkeypatch.setattr(engine, "dispatch", fail_second)
    with pytest.raises(RuntimeError, match="before truck transition"):
        start_transfer(persistence, engine, entities=entities)

    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    assert shipment is not None and shipment.state == "in_transit"
    assert truck is not None and truck.state == "scheduled"

    recovered = _fresh_engine(persistence)
    assert start_transfer(persistence, recovered, entities=entities) is True
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    assert truck is not None and truck.state == "in_transit"


def test_complete_unload_recovers_after_stock_move_before_terminal_transition(monkeypatch) -> None:
    persistence, engine, entities = _runtime(origin_on_hand=20.0, transfer_quantity=8.0)
    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=2)
    assert arrive_truck(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=3)

    def fail_dispatch(_command):
        raise RuntimeError("simulated crash after stock move")

    monkeypatch.setattr(engine, "dispatch", fail_dispatch)
    with pytest.raises(RuntimeError, match="after stock move"):
        complete_unload(persistence, engine, entities=entities)

    origin_stock = persistence.entity("warehouse_management_stock", entities.origin_stock_id)
    destination_stock = persistence.entity("warehouse_management_stock", entities.destination_stock_id)
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert origin_stock is not None and origin_stock.attributes["on_hand"] == pytest.approx(12.0)
    assert destination_stock is not None and destination_stock.attributes["on_hand"] == pytest.approx(8.0)
    assert shipment is not None and shipment.state == "handling"
    assert shipment.attributes["stock_moved"] is True

    recovered = _fresh_engine(persistence, now=ORIGIN + timedelta(hours=3))
    assert complete_unload(persistence, recovered, entities=entities) is True
    destination_stock = persistence.entity("warehouse_management_stock", entities.destination_stock_id)
    assert destination_stock is not None and destination_stock.attributes["on_hand"] == pytest.approx(8.0)


def test_completed_shipment_reconciles_outstanding_resource_releases(monkeypatch) -> None:
    persistence, engine, entities = _runtime()
    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=2)
    assert arrive_truck(persistence, engine, entities=entities) is True
    assert assign_forklift(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=3)

    original_dispatch = engine.dispatch
    dispatches = 0

    def fail_first_release(command):
        nonlocal dispatches
        dispatches += 1
        if dispatches == 2:
            raise RuntimeError("simulated crash during release")
        original_dispatch(command)

    monkeypatch.setattr(engine, "dispatch", fail_first_release)
    with pytest.raises(RuntimeError, match="during release"):
        complete_unload(persistence, engine, entities=entities)

    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    forklift = persistence.entity("warehouse_management_forklift", entities.forklift_ids[0])
    assert shipment is not None and shipment.state == "completed"
    assert shipment.attributes["resources_released"] is False
    assert forklift is not None and forklift.state == "assigned"

    recovered = _fresh_engine(persistence, now=ORIGIN + timedelta(hours=3))
    assert complete_unload(persistence, recovered, entities=entities) is True

    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    dock = persistence.entity("warehouse_management_dock", entities.dock_ids[0])
    forklift = persistence.entity("warehouse_management_forklift", entities.forklift_ids[0])
    assert shipment is not None and shipment.attributes["resources_released"] is True
    assert truck is not None and truck.state == "released"
    assert dock is not None and dock.state == "available"
    assert forklift is not None and forklift.state == "available"
