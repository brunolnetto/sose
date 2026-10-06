from __future__ import annotations

from datetime import timedelta

import pytest

from sose.examples.warehouse_management.config import WarehouseManagementConfig
from sose.examples.warehouse_management.definition import _reconcile_tick
from sose.examples.warehouse_management.simulation import (
    ORIGIN,
    arrive_truck,
    build_runtime,
    seed_reference,
    start_transfer,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(**config_overrides):
    persistence = MemoryPersistence()
    config = WarehouseManagementConfig(**config_overrides)
    entities = seed_reference(
        persistence,
        now=config.start_at,
        origin_on_hand=config.origin_on_hand,
        transfer_quantity=config.transfer_quantity,
        dock_count=config.dock_count,
        forklift_count=config.forklift_count,
        planned_completion_hours=config.planned_completion_hours,
    )
    _, engine = build_runtime(
        persistence,
        now=config.start_at,
        step=config.tick_step,
        random_seed=config.random_seed,
    )
    return persistence, engine, config, entities


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def test_reconcile_can_be_disabled_without_mutating_reference_flow() -> None:
    persistence, engine, config, entities = _runtime(auto_progress_transfer=False)
    _reconcile_tick(persistence, engine, None, config, entities)
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "planned"


def test_reconcile_requires_configured_shipment_to_exist() -> None:
    persistence, engine, config, entities = _runtime()
    missing = type(entities)(
        origin_site_id=entities.origin_site_id,
        destination_site_id=entities.destination_site_id,
        dock_ids=entities.dock_ids,
        forklift_ids=entities.forklift_ids,
        truck_id=entities.truck_id,
        origin_stock_id=entities.origin_stock_id,
        destination_stock_id=entities.destination_stock_id,
        shipment_id="missing-shipment",
    )
    with pytest.raises(RuntimeError, match="configured warehouse shipment was not persisted"):
        _reconcile_tick(persistence, engine, None, config, missing)


def test_reconcile_finishes_partial_startup_before_advancing_to_arrival(monkeypatch) -> None:
    persistence, engine, config, entities = _runtime()
    original_dispatch = engine.dispatch
    dispatches = 0

    def fail_truck_transition(command):
        nonlocal dispatches
        dispatches += 1
        if dispatches == 2:
            raise RuntimeError("crash-before-truck")
        original_dispatch(command)

    monkeypatch.setattr(engine, "dispatch", fail_truck_transition)
    with pytest.raises(RuntimeError, match="crash-before-truck"):
        start_transfer(persistence, engine, entities=entities)

    _, recovered = build_runtime(persistence, now=ORIGIN + timedelta(hours=1))
    _reconcile_tick(persistence, recovered, None, config, entities)
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    truck = persistence.entity("warehouse_management_truck", entities.truck_id)
    assert shipment is not None and shipment.state == "in_transit"
    assert truck is not None and truck.state == "in_transit"


def test_reconcile_retries_delayed_dock_assignment_when_capacity_returns() -> None:
    persistence, engine, config, entities = _runtime(dock_count=1)
    dock = persistence.entity("warehouse_management_dock", entities.dock_ids[0])
    assert dock is not None
    dock.state = "occupied"
    _save(persistence, dock)

    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=1)
    assert arrive_truck(persistence, engine, entities=entities) is False

    dock = persistence.entity("warehouse_management_dock", entities.dock_ids[0])
    assert dock is not None
    dock.state = "available"
    _save(persistence, dock)

    _reconcile_tick(persistence, engine, None, config, entities)
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "docked"


def test_reconcile_progresses_docked_handling_and_completed_cleanup() -> None:
    persistence, engine, config, entities = _runtime()
    assert start_transfer(persistence, engine, entities=entities) is True
    engine.context.clock.now = ORIGIN + timedelta(hours=1)
    assert arrive_truck(persistence, engine, entities=entities) is True

    _reconcile_tick(persistence, engine, None, config, entities)
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "handling"

    engine.context.clock.now = ORIGIN + timedelta(hours=2)
    _reconcile_tick(persistence, engine, None, config, entities)
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "completed"
    assert shipment.attributes["resources_released"] is True

    _reconcile_tick(persistence, engine, None, config, entities)
    shipment = persistence.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment is not None and shipment.attributes["resources_released"] is True
