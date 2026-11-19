"""Unload completion and handling-resource release (split from simulation.py)."""

from __future__ import annotations

from sose.core.engine import Engine
from sose.persistence.base import Persistence

from .records import WarehouseManagementEntities, _dispatch, _entity, _save


def _dispatch_handling_releases(
    persistence: Persistence,
    engine: Engine,
    *,
    entities: WarehouseManagementEntities,
    shipment,
    forklift_id,
    dock_id,
) -> None:
    forklift = _entity(persistence, "warehouse_management_forklift", str(forklift_id))
    if forklift.state == "assigned":
        _dispatch(
            engine, forklift, "release", key=("forklift", forklift.id, shipment.id, "release")
        )

    dock = _entity(persistence, "warehouse_management_dock", str(dock_id))
    if dock.state in {"occupied", "reserved"}:
        _dispatch(engine, dock, "release", key=("dock", dock.id, shipment.id, "release"))

    truck = _entity(persistence, "warehouse_management_truck", entities.truck_id)
    if truck.state == "docked":
        _dispatch(engine, truck, "release", key=("truck", truck.id, "release"))


def _release_completed_resources(
    persistence: Persistence,
    engine: Engine,
    *,
    entities: WarehouseManagementEntities,
) -> bool:
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    if shipment.state != "completed":
        return False
    if bool(shipment.attributes.get("resources_released", False)):
        return True

    forklift_id = shipment.attributes.get("assigned_forklift_id")
    dock_id = shipment.attributes.get("assigned_dock_id")
    if not forklift_id or not dock_id:
        raise RuntimeError("completed shipment is missing durable handling ownership")

    _dispatch_handling_releases(
        persistence,
        engine,
        entities=entities,
        shipment=shipment,
        forklift_id=forklift_id,
        dock_id=dock_id,
    )

    forklift = _entity(persistence, "warehouse_management_forklift", str(forklift_id))
    dock = _entity(persistence, "warehouse_management_dock", str(dock_id))
    truck = _entity(persistence, "warehouse_management_truck", entities.truck_id)
    released = (
        forklift.state == "available"
        and dock.state == "available"
        and truck.state == "released"
    )
    if released:
        shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
        shipment.attributes["resources_released"] = True
        _save(persistence, shipment)
    return released


def complete_unload(
    persistence: Persistence,
    engine: Engine,
    *,
    entities: WarehouseManagementEntities,
) -> bool:
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    if shipment.state == "completed":
        return _release_completed_resources(persistence, engine, entities=entities)
    if shipment.state != "handling":
        return False

    dock_id = shipment.attributes.get("assigned_dock_id")
    forklift_id = shipment.attributes.get("assigned_forklift_id")
    if not dock_id or not forklift_id:
        raise RuntimeError("handling shipment is missing dock or forklift ownership")

    if not bool(shipment.attributes.get("stock_moved", False)):
        origin_stock = _entity(persistence, "warehouse_management_stock", entities.origin_stock_id)
        destination_stock = _entity(
            persistence, "warehouse_management_stock", entities.destination_stock_id
        )
        quantity = float(shipment.attributes["quantity"])
        reserved = float(origin_stock.attributes.get("reserved", 0.0))
        on_hand = float(origin_stock.attributes["on_hand"])
        if reserved < quantity or on_hand < quantity:
            raise RuntimeError("reserved transfer stock is inconsistent with shipment quantity")

        origin_stock.attributes["reserved"] = reserved - quantity
        origin_stock.attributes["on_hand"] = on_hand - quantity
        destination_stock.attributes["on_hand"] = (
            float(destination_stock.attributes["on_hand"]) + quantity
        )
        shipment.attributes["stock_moved"] = True
        if shipment.attributes.get("completed_at") is None:
            shipment.attributes["completed_at"] = engine.context.clock.now.isoformat()
        _save(persistence, origin_stock, destination_stock, shipment)
        shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)

    if shipment.state == "handling":
        _dispatch(engine, shipment, "complete", key=("shipment", shipment.id, "complete"))

    return _release_completed_resources(persistence, engine, entities=entities)
