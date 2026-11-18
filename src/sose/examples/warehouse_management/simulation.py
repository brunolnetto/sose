from __future__ import annotations

from datetime import datetime, timedelta  # noqa: F401

from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.base import Persistence

from .entities import Dock, Forklift, Shipment, StockPosition, Truck, WarehouseSite
from .statecharts import (  # noqa: F401 - historical import location
    DockChart,
    ForkliftChart,
    ShipmentChart,
    TruckChart,
    WarehouseSiteChart,
)
from .records import (  # noqa: F401 - historical import location
    ORIGIN,
    REFERENCE_SKU,
    REGISTERED_ENTITY_TYPES,
    WarehouseManagementEntities,
    _dispatch,
    _entity,
    _save,
    build_runtime,
)
from .external_stock import (  # noqa: F401 - historical import location
    consume_external_reservation,
    receive_external_replenishment,
    reserve_external_stock,
)
from .unloading import (  # noqa: F401 - historical import location
    _release_completed_resources,
    complete_unload,
)


def _validate_seed_arguments(
    *,
    origin_on_hand: float,
    transfer_quantity: float,
    sku: str,
    dock_count: int,
    forklift_count: int,
    planned_completion_hours: float,
    instance_key: str | None,
) -> None:
    if origin_on_hand < 0:
        raise ValueError("origin_on_hand cannot be negative")
    if not sku:
        raise ValueError("sku cannot be empty")
    if transfer_quantity <= 0:
        raise ValueError("transfer_quantity must be positive")
    if dock_count < 1:
        raise ValueError("dock_count must be at least one")
    if forklift_count < 1:
        raise ValueError("forklift_count must be at least one")
    if planned_completion_hours <= 0:
        raise ValueError("planned_completion_hours must be positive")

    if instance_key is not None and (
        not isinstance(instance_key, str) or not instance_key.strip()
    ):
        raise ValueError("instance_key must be a nonempty string")


def seed_reference(
    persistence: Persistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    origin_on_hand: float = 20.0,
    transfer_quantity: float = 8.0,
    sku: str = REFERENCE_SKU,
    dock_count: int = 2,
    forklift_count: int = 2,
    planned_completion_hours: float = 4.0,
    instance_key: str | None = None,
) -> WarehouseManagementEntities:
    _validate_seed_arguments(
        origin_on_hand=origin_on_hand,
        transfer_quantity=transfer_quantity,
        sku=sku,
        dock_count=dock_count,
        forklift_count=forklift_count,
        planned_completion_hours=planned_completion_hours,
        instance_key=instance_key,
    )
    prefix = ("warehouse-management",) if instance_key is None else ("warehouse-management", instance_key)
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
    origin_site = context.entities.create(
        WarehouseSite,
        key=(*prefix, "site-origin"),
        state="operational",
        attributes={"code": "SITE-A", "name": "Origin DC"},
    )
    destination_site = context.entities.create(
        WarehouseSite,
        key=(*prefix, "site-destination"),
        state="operational",
        attributes={"code": "SITE-B", "name": "Destination DC"},
    )

    docks = tuple(
        context.entities.create(
            Dock,
            key=(*prefix, "dock", index),
            state="available",
            attributes={"site_id": destination_site.id, "dock_code": f"D-{index + 1}"},
        )
        for index in range(dock_count)
    )
    forklifts = tuple(
        context.entities.create(
            Forklift,
            key=(*prefix, "forklift", index),
            state="available",
            attributes={"site_id": destination_site.id, "forklift_code": f"FL-{index + 1}"},
        )
        for index in range(forklift_count)
    )
    truck = context.entities.create(
        Truck,
        key=(*prefix, "truck-1"),
        state="scheduled",
        attributes={"truck_code": "TRK-1", "shipment_id": None},
    )
    origin_stock = context.entities.create(
        StockPosition,
        key=(*prefix, origin_site.id, sku),
        state="available",
        attributes={
            "site_id": origin_site.id,
            "sku": sku,
            "on_hand": float(origin_on_hand),
            "reserved": 0.0,
        },
    )
    destination_stock = context.entities.create(
        StockPosition,
        key=(*prefix, destination_site.id, sku),
        state="available",
        attributes={
            "site_id": destination_site.id,
            "sku": sku,
            "on_hand": 0.0,
            "reserved": 0.0,
        },
    )
    shipment = context.entities.create(
        Shipment,
        key=(*prefix, "shipment-1"),
        state="planned",
        attributes={
            "origin_site_id": origin_site.id,
            "destination_site_id": destination_site.id,
            "truck_id": truck.id,
            "sku": sku,
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


# Moved by the quality-ratchet splits (#632); importing these names from here
# is deprecated (ADR-0011). Import them from the module named on the right.
from sose._deprecation import deprecated_reexports as _deprecated_reexports

__getattr__ = _deprecated_reexports(__name__, {
    "deterministic_id": "sose.core.identity",
    "matching_reservation": "sose.examples.warehouse_management.reservations",
    "shipment_kpis": "sose.examples.warehouse_management.kpis",
    "validated_consumption_request": "sose.examples.warehouse_management.reservations",
}, since="0.9.0", removed_in="0.10.0")
