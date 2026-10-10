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
    sku: str = REFERENCE_SKU,
    dock_count: int = 2,
    forklift_count: int = 2,
    planned_completion_hours: float = 4.0,
    instance_key: str | None = None,
) -> WarehouseManagementEntities:
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

    if instance_key is not None and (not isinstance(instance_key, str) or not instance_key.strip()):
        raise ValueError("instance_key must be a nonempty string")
    prefix = ("warehouse-management",) if instance_key is None else ("warehouse-management", instance_key)
    context, _ = build_runtime(persistence, now=now)
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


def _entity(persistence: Persistence, entity_type: str, entity_id: str):
    entity = persistence.entity(entity_type, entity_id)
    if entity is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return entity


def _save(persistence: Persistence, *entities) -> None:
    with persistence.transaction() as uow:
        for entity in entities:
            uow.save_entity(entity)


def receive_external_replenishment(
    persistence: Persistence,
    engine: Engine,
    *,
    stock_id: str,
    quantity: float,
    sku: str,
    receipt_reference: str,
    caused_by=None,
    correlation_id: str | None = None,
) -> bool:
    """Idempotently apply a foreign procurement receipt to WM-owned stock truth."""

    if quantity <= 0:
        raise ValueError("replenishment quantity must be positive")
    if not sku:
        raise ValueError("replenishment sku cannot be empty")
    if not receipt_reference:
        raise ValueError("receipt_reference cannot be empty")

    stock = _entity(persistence, "warehouse_management_stock", stock_id)
    stock_sku = str(stock.attributes.get("sku", ""))
    if stock_sku != sku:
        raise ValueError(
            f"replenishment SKU mismatch: stock={stock_sku!r}, receipt={sku!r}"
        )
    receipts = dict(stock.attributes.get("external_receipts", {}))
    existing = receipts.get(receipt_reference)
    if existing is not None:
        if float(existing) != float(quantity):
            raise ValueError(
                f"receipt replay conflict: {receipt_reference}"
            )
        return False

    stock.attributes["on_hand"] = float(stock.attributes["on_hand"]) + float(quantity)
    receipts[receipt_reference] = float(quantity)
    stock.attributes["external_receipts"] = receipts

    event = engine.context.events.create(
        "warehouse_management.replenishment_received",
        entity=stock,
        caused_by=caused_by,
        correlation_id=correlation_id,
        key=("warehouse-management", stock.id, "external-receipt", receipt_reference),
        receipt_reference=receipt_reference,
        sku=sku,
        quantity=float(quantity),
    )
    with persistence.transaction() as uow:
        uow.save_entity(stock)
        uow.append_event(event)
    return True



def reserve_external_stock(
    persistence: Persistence,
    engine: Engine,
    *,
    stock_id: str,
    quantity: float,
    sku: str,
    reservation_reference: str,
    caused_by=None,
    correlation_id: str | None = None,
) -> bool:
    """Idempotently reserve WM-owned stock for a foreign process.

    The foreign process receives only the reservation/stock references. Warehouse
    Management remains authoritative for on-hand and reserved quantities.
    """

    if quantity <= 0:
        raise ValueError("reservation quantity must be positive")
    normalized_quantity = round(float(quantity), 6)
    if normalized_quantity <= 0:
        raise ValueError("reservation quantity is below supported precision")
    if not sku:
        raise ValueError("reservation sku cannot be empty")
    if not reservation_reference:
        raise ValueError("reservation_reference cannot be empty")

    stock = _entity(persistence, "warehouse_management_stock", stock_id)
    stock_sku = str(stock.attributes.get("sku", ""))
    if stock_sku != sku:
        raise ValueError(
            f"reservation SKU mismatch: stock={stock_sku!r}, request={sku!r}"
        )

    reservations = dict(stock.attributes.get("external_reservations", {}))
    existing = reservations.get(reservation_reference)
    expected = {
        "sku": sku,
        "quantity": normalized_quantity,
        "consumed": False,
        "consumption_reference": None,
    }
    if existing is not None:
        existing_record = dict(existing)
        if (
            str(existing_record.get("sku")) != sku
            or float(existing_record.get("quantity", 0.0)) != normalized_quantity
        ):
            raise ValueError(
                f"reservation replay conflict: {reservation_reference}"
            )
        return False

    on_hand = float(stock.attributes.get("on_hand", 0.0))
    reserved = float(stock.attributes.get("reserved", 0.0))
    if on_hand - reserved + 1e-9 < normalized_quantity:
        return False

    stock.attributes["reserved"] = round(reserved + normalized_quantity, 6)
    reservations[reservation_reference] = expected
    stock.attributes["external_reservations"] = reservations

    event = engine.context.events.create(
        "warehouse_management.inventory_reserved",
        entity=stock,
        caused_by=caused_by,
        correlation_id=correlation_id,
        key=("warehouse-management", stock.id, "external-reservation", reservation_reference),
        reservation_reference=reservation_reference,
        sku=sku,
        quantity=normalized_quantity,
    )
    with persistence.transaction() as uow:
        uow.save_entity(stock)
        uow.append_event(event)
    return True


def consume_external_reservation(
    persistence: Persistence,
    engine: Engine,
    *,
    stock_id: str,
    quantity: float,
    sku: str,
    reservation_reference: str,
    consumption_reference: str,
    caused_by=None,
    correlation_id: str | None = None,
) -> bool:
    """Idempotently consume a previously reserved foreign-process quantity."""

    if quantity <= 0:
        raise ValueError("consumption quantity must be positive")
    normalized_quantity = round(float(quantity), 6)
    if normalized_quantity <= 0:
        raise ValueError("consumption quantity is below supported precision")
    if not sku:
        raise ValueError("consumption sku cannot be empty")
    if not reservation_reference:
        raise ValueError("reservation_reference cannot be empty")
    if not consumption_reference:
        raise ValueError("consumption_reference cannot be empty")

    stock = _entity(persistence, "warehouse_management_stock", stock_id)
    stock_sku = str(stock.attributes.get("sku", ""))
    if stock_sku != sku:
        raise ValueError(
            f"consumption SKU mismatch: stock={stock_sku!r}, request={sku!r}"
        )

    reservations = dict(stock.attributes.get("external_reservations", {}))
    existing = reservations.get(reservation_reference)
    if existing is None:
        raise ValueError(f"unknown external reservation: {reservation_reference}")

    record = dict(existing)
    if (
        str(record.get("sku")) != sku
        or float(record.get("quantity", 0.0)) != normalized_quantity
    ):
        raise ValueError(
            f"reservation consumption conflict: {reservation_reference}"
        )

    if bool(record.get("consumed", False)):
        if record.get("consumption_reference") != consumption_reference:
            raise ValueError(
                f"consumption replay conflict: {reservation_reference}"
            )
        return False

    on_hand = float(stock.attributes.get("on_hand", 0.0))
    reserved = float(stock.attributes.get("reserved", 0.0))
    if reserved + 1e-9 < normalized_quantity or on_hand + 1e-9 < normalized_quantity:
        raise RuntimeError("reserved stock projection cannot satisfy consumption")

    stock.attributes["on_hand"] = round(on_hand - normalized_quantity, 6)
    stock.attributes["reserved"] = round(reserved - normalized_quantity, 6)
    record["consumed"] = True
    record["consumption_reference"] = consumption_reference
    reservations[reservation_reference] = record
    stock.attributes["external_reservations"] = reservations

    event = engine.context.events.create(
        "warehouse_management.inventory_consumed",
        entity=stock,
        caused_by=caused_by,
        correlation_id=correlation_id,
        key=(
            "warehouse-management",
            stock.id,
            "external-consumption",
            consumption_reference,
        ),
        reservation_reference=reservation_reference,
        consumption_reference=consumption_reference,
        sku=sku,
        quantity=normalized_quantity,
    )
    with persistence.transaction() as uow:
        uow.save_entity(stock)
        uow.append_event(event)
    return True

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
