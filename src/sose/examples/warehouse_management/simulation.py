from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.base import Persistence

from .entities import Dock, Forklift, Shipment, StockPosition, Truck, WarehouseSite
from .statecharts import DockChart, ForkliftChart, ShipmentChart, TruckChart, WarehouseSiteChart


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
REFERENCE_SKU = "widget-a"


@dataclass(frozen=True, slots=True)
class WarehouseManagementEntities:
    origin_site_id: str
    destination_site_id: str
    dock_ids: tuple[str, ...]
    forklift_ids: tuple[str, ...]
    truck_id: str
    origin_stock_id: str
    destination_stock_id: str
    shipment_id: str


def _flow_id(shipment_id: str) -> str:
    return deterministic_id("warehouse-management-flow", shipment_id)


def build_runtime(
    persistence: Persistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    step: timedelta = timedelta(hours=1),
    random_seed: int = 5505,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("warehouse_management_site", WarehouseSiteChart))
    registry.register(EntityType("warehouse_management_dock", DockChart))
    registry.register(EntityType("warehouse_management_truck", TruckChart))
    registry.register(EntityType("warehouse_management_forklift", ForkliftChart))
    registry.register(EntityType("warehouse_management_shipment", ShipmentChart))
    return context, Engine(context=context, registry=registry, persistence=persistence)


def seed_reference(
    persistence: Persistence,
    *,
    now: datetime = ORIGIN,
    origin_on_hand: float = 20.0,
    transfer_quantity: float = 8.0,
    dock_count: int = 2,
    forklift_count: int = 2,
    planned_completion_hours: float = 4.0,
) -> WarehouseManagementEntities:
    if origin_on_hand < 0:
        raise ValueError("origin_on_hand cannot be negative")
    if transfer_quantity <= 0:
        raise ValueError("transfer_quantity must be positive")
    if dock_count < 1:
        raise ValueError("dock_count must be at least one")
    if forklift_count < 1:
        raise ValueError("forklift_count must be at least one")
    if planned_completion_hours <= 0:
        raise ValueError("planned_completion_hours must be positive")

    context, _ = build_runtime(persistence, now=now)
    origin_site = context.entities.create(
        WarehouseSite,
        key=("warehouse-management", "site-origin"),
        state="operational",
        attributes={"code": "SITE-A", "name": "Origin DC"},
    )
    destination_site = context.entities.create(
        WarehouseSite,
        key=("warehouse-management", "site-destination"),
        state="operational",
        attributes={"code": "SITE-B", "name": "Destination DC"},
    )

    docks = tuple(
        context.entities.create(
            Dock,
            key=("warehouse-management", "dock", index),
            state="available",
            attributes={"site_id": destination_site.id, "dock_code": f"D-{index + 1}"},
        )
        for index in range(dock_count)
    )
    forklifts = tuple(
        context.entities.create(
            Forklift,
            key=("warehouse-management", "forklift", index),
            state="available",
            attributes={"site_id": destination_site.id, "forklift_code": f"FL-{index + 1}"},
        )
        for index in range(forklift_count)
    )
    truck = context.entities.create(
        Truck,
        key=("warehouse-management", "truck-1"),
        state="scheduled",
        attributes={"truck_code": "TRK-1", "shipment_id": None},
    )
    origin_stock = context.entities.create(
        StockPosition,
        key=("warehouse-management", origin_site.id, REFERENCE_SKU),
        state="available",
        attributes={
            "site_id": origin_site.id,
            "sku": REFERENCE_SKU,
            "on_hand": float(origin_on_hand),
            "reserved": 0.0,
        },
    )
    destination_stock = context.entities.create(
        StockPosition,
        key=("warehouse-management", destination_site.id, REFERENCE_SKU),
        state="available",
        attributes={
            "site_id": destination_site.id,
            "sku": REFERENCE_SKU,
            "on_hand": 0.0,
            "reserved": 0.0,
        },
    )
    shipment = context.entities.create(
        Shipment,
        key=("warehouse-management", "shipment-1"),
        state="planned",
        attributes={
            "origin_site_id": origin_site.id,
            "destination_site_id": destination_site.id,
            "truck_id": truck.id,
            "sku": REFERENCE_SKU,
            "quantity": float(transfer_quantity),
            "planned_completion_at": (now + timedelta(hours=planned_completion_hours)).isoformat(),
            "departed_at": None,
            "arrived_at": None,
            "completed_at": None,
            "assigned_dock_id": None,
            "assigned_forklift_id": None,
            "stock_reserved": False,
            "stock_moved": False,
            "resources_released": False,
        },
    )
    truck.attributes["shipment_id"] = shipment.id

    with persistence.transaction() as uow:
        for entity in (
            origin_site,
            destination_site,
            *docks,
            *forklifts,
            truck,
            origin_stock,
            destination_stock,
            shipment,
        ):
            uow.save_entity(entity)

    return WarehouseManagementEntities(
        origin_site_id=origin_site.id,
        destination_site_id=destination_site.id,
        dock_ids=tuple(dock.id for dock in docks),
        forklift_ids=tuple(forklift.id for forklift in forklifts),
        truck_id=truck.id,
        origin_stock_id=origin_stock.id,
        destination_stock_id=destination_stock.id,
        shipment_id=shipment.id,
    )


def _entity(persistence: Persistence, entity_type: str, entity_id: str):
    entity = persistence.entity(entity_type, entity_id)
    if entity is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return entity


def _save(persistence: Persistence, *entities) -> None:
    with persistence.transaction() as uow:
        for entity in entities:
            uow.save_entity(entity)


def _dispatch(engine: Engine, entity, event: str, *, key: tuple[object, ...]) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=_flow_id(str(entity.attributes.get("shipment_id") or entity.id)),
        key=key,
    )
    engine.dispatch(command)


def start_transfer(
    persistence: Persistence,
    engine: Engine,
    *,
    entities: WarehouseManagementEntities,
) -> bool:
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    truck = _entity(persistence, "warehouse_management_truck", entities.truck_id)
    stock = _entity(persistence, "warehouse_management_stock", entities.origin_stock_id)

    if shipment.state not in {"planned", "in_transit"}:
        return shipment.state != "planned"
    if truck.state not in {"scheduled", "in_transit"}:
        return truck.state != "scheduled"

    quantity = float(shipment.attributes["quantity"])
    stock_reserved = bool(shipment.attributes.get("stock_reserved", False))
    if not stock_reserved:
        if shipment.state != "planned":
            raise RuntimeError("in-transit shipment is missing durable stock reservation")
        available = float(stock.attributes["on_hand"]) - float(stock.attributes.get("reserved", 0.0))
        if available < quantity:
            return False
        stock.attributes["reserved"] = float(stock.attributes.get("reserved", 0.0)) + quantity
        shipment.attributes["stock_reserved"] = True
        if shipment.attributes.get("departed_at") is None:
            shipment.attributes["departed_at"] = engine.context.clock.now.isoformat()
        _save(persistence, stock, shipment)
        shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)

    if shipment.state == "planned":
        _dispatch(engine, shipment, "start_transit", key=("shipment", shipment.id, "start"))

    truck = _entity(persistence, "warehouse_management_truck", entities.truck_id)
    if truck.state == "scheduled":
        _dispatch(engine, truck, "depart_origin", key=("truck", truck.id, "depart"))
    return True


def _available_dock(persistence: Persistence, entities: WarehouseManagementEntities):
    for dock_id in entities.dock_ids:
        dock = _entity(persistence, "warehouse_management_dock", dock_id)
        if dock.state == "available":
            return dock
    return None


def arrive_truck(
    persistence: Persistence,
    engine: Engine,
    *,
    entities: WarehouseManagementEntities,
) -> bool:
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    truck = _entity(persistence, "warehouse_management_truck", entities.truck_id)
    if shipment.state == "completed":
        return True
    if shipment.state == "in_transit":
        if shipment.attributes.get("arrived_at") is None:
            shipment.attributes["arrived_at"] = engine.context.clock.now.isoformat()
            _save(persistence, shipment)
        _dispatch(engine, shipment, "arrive", key=("shipment", shipment.id, "arrive"))
        shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)

    dock = _available_dock(persistence, entities)
    if dock is None:
        if truck.state == "in_transit":
            _dispatch(engine, truck, "wait_for_dock", key=("truck", truck.id, "wait-dock"))
        if shipment.state == "arrived":
            _dispatch(engine, shipment, "delay", key=("shipment", shipment.id, "dock-delay"))
        return False

    if shipment.state == "delayed":
        _dispatch(engine, shipment, "resume", key=("shipment", shipment.id, "resume"))
        shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    if shipment.state != "arrived":
        return shipment.state in {"docked", "handling", "completed"}

    _dispatch(engine, dock, "reserve", key=("dock", dock.id, shipment.id, "reserve"))
    dock = _entity(persistence, "warehouse_management_dock", dock.id)
    _dispatch(engine, dock, "occupy", key=("dock", dock.id, shipment.id, "occupy"))
    truck = _entity(persistence, "warehouse_management_truck", entities.truck_id)
    if truck.state in {"in_transit", "waiting_dock"}:
        _dispatch(engine, truck, "dock", key=("truck", truck.id, "dock"))
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    _dispatch(engine, shipment, "dock", key=("shipment", shipment.id, "dock"))
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    shipment.attributes["assigned_dock_id"] = dock.id
    _save(persistence, shipment)
    return True


def _available_forklift(persistence: Persistence, entities: WarehouseManagementEntities):
    for forklift_id in entities.forklift_ids:
        forklift = _entity(persistence, "warehouse_management_forklift", forklift_id)
        if forklift.state == "available":
            return forklift
    return None


def assign_forklift(
    persistence: Persistence,
    engine: Engine,
    *,
    entities: WarehouseManagementEntities,
) -> bool:
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    if shipment.state == "handling":
        return True
    if shipment.state != "docked":
        return False
    forklift = _available_forklift(persistence, entities)
    if forklift is None:
        return False

    _dispatch(engine, forklift, "assign", key=("forklift", forklift.id, shipment.id, "assign"))
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    _dispatch(engine, shipment, "start_handling", key=("shipment", shipment.id, "handling"))
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    shipment.attributes["assigned_forklift_id"] = forklift.id
    _save(persistence, shipment)
    return True


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

    forklift = _entity(persistence, "warehouse_management_forklift", str(forklift_id))
    if forklift.state == "assigned":
        _dispatch(engine, forklift, "release", key=("forklift", forklift.id, shipment.id, "release"))

    dock = _entity(persistence, "warehouse_management_dock", str(dock_id))
    if dock.state in {"occupied", "reserved"}:
        _dispatch(engine, dock, "release", key=("dock", dock.id, shipment.id, "release"))

    truck = _entity(persistence, "warehouse_management_truck", entities.truck_id)
    if truck.state == "docked":
        _dispatch(engine, truck, "release", key=("truck", truck.id, "release"))

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
        destination_stock = _entity(persistence, "warehouse_management_stock", entities.destination_stock_id)
        quantity = float(shipment.attributes["quantity"])
        reserved = float(origin_stock.attributes.get("reserved", 0.0))
        on_hand = float(origin_stock.attributes["on_hand"])
        if reserved < quantity or on_hand < quantity:
            raise RuntimeError("reserved transfer stock is inconsistent with shipment quantity")

        origin_stock.attributes["reserved"] = reserved - quantity
        origin_stock.attributes["on_hand"] = on_hand - quantity
        destination_stock.attributes["on_hand"] = float(destination_stock.attributes["on_hand"]) + quantity
        shipment.attributes["stock_moved"] = True
        if shipment.attributes.get("completed_at") is None:
            shipment.attributes["completed_at"] = engine.context.clock.now.isoformat()
        _save(persistence, origin_stock, destination_stock, shipment)
        shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)

    if shipment.state == "handling":
        _dispatch(engine, shipment, "complete", key=("shipment", shipment.id, "complete"))

    return _release_completed_resources(persistence, engine, entities=entities)


def shipment_kpis(
    persistence: Persistence,
    *,
    entities: WarehouseManagementEntities,
) -> dict[str, object]:
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    departed_raw = shipment.attributes.get("departed_at")
    completed_raw = shipment.attributes.get("completed_at")
    planned_raw = shipment.attributes.get("planned_completion_at")

    departed_at = datetime.fromisoformat(str(departed_raw)) if departed_raw else None
    completed_at = datetime.fromisoformat(str(completed_raw)) if completed_raw else None
    planned_at = datetime.fromisoformat(str(planned_raw)) if planned_raw else None
    completed = shipment.state == "completed" and completed_at is not None
    lead_time = (
        (completed_at - departed_at).total_seconds()
        if completed_at is not None and departed_at is not None
        else None
    )
    lateness = (
        max(0.0, (completed_at - planned_at).total_seconds())
        if completed_at is not None and planned_at is not None
        else None
    )
    return {
        "shipment_state": shipment.state,
        "completed": completed,
        "on_time": completed_at <= planned_at if completed_at is not None and planned_at is not None else None,
        "lead_time_seconds": lead_time,
        "lateness_seconds": lateness,
        "assigned_dock_id": shipment.attributes.get("assigned_dock_id"),
        "assigned_forklift_id": shipment.attributes.get("assigned_forklift_id"),
    }
