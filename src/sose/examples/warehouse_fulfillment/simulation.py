from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import (
    Allocation,
    FulfillmentOrder,
    FulfillmentServiceTask,
    InventoryLot,
    InventoryOccurrence,
)
from .scenarios import ORIGIN
from .statecharts import (
    AllocationChart,
    FulfillmentOrderChart,
    FulfillmentServiceTaskChart,
    InventoryOccurrenceChart,
)


PRIMARY_SKU = "widget-a"
SUBSTITUTE_SKU = "widget-b"

PICKER_RESOURCE = "fulfillment_picker"
PACKING_RESOURCE = "packing_station"
SHIPPING_RESOURCE = "shipping_dock"


@dataclass(frozen=True, slots=True)
class WarehouseEntities:
    order_id: str
    lot_ids: tuple[str, ...]


def flow_correlation_id(order_id: str) -> str:
    return deterministic_id("warehouse-flow", order_id)


def allocation_id(order_id: str, lot_id: str) -> str:
    return deterministic_id(
        "entity",
        "warehouse_allocation",
        "warehouse-reference",
        order_id,
        lot_id,
    )


def occurrence_id(kind: str, subject_id: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "warehouse_inventory_occurrence",
        "warehouse-reference",
        kind,
        subject_id,
        sequence,
    )


def service_task_id(stage: str, subject_id: str) -> str:
    return deterministic_id(
        "entity",
        "warehouse_fulfillment_service_task",
        "warehouse-reference",
        "service",
        stage,
        subject_id,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 1429,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(
        EntityType("warehouse_fulfillment_order", FulfillmentOrderChart)
    )
    registry.register(EntityType("warehouse_allocation", AllocationChart))
    registry.register(
        EntityType("warehouse_inventory_occurrence", InventoryOccurrenceChart)
    )
    registry.register(
        EntityType("warehouse_fulfillment_service_task", FulfillmentServiceTaskChart)
    )
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    requested_quantity: float = 10.0,
    primary_on_hand: float = 6.0,
    substitute_on_hand: float = 5.0,
    allow_substitute: bool = True,
    picker_capacity: int = 1,
    packing_station_capacity: int = 1,
    shipping_dock_capacity: int = 1,
) -> WarehouseEntities:
    context, _ = build_runtime(persistence, now=now)
    order = context.entities.create(
        FulfillmentOrder,
        key=("warehouse-reference", "order-1"),
        state="requested",
        attributes={
            "requested_sku": PRIMARY_SKU,
            "acceptable_skus": (
                [PRIMARY_SKU, SUBSTITUTE_SKU]
                if allow_substitute
                else [PRIMARY_SKU]
            ),
            "requested_quantity": requested_quantity,
            "allocation_ids": [],
            "occurrence_ids": [],
        },
    )
    primary = context.entities.create(
        InventoryLot,
        key=("warehouse-reference", "lot-a"),
        state="available",
        attributes={
            "lot_key": "lot-a",
            "sku": PRIMARY_SKU,
            "on_hand": primary_on_hand,
            "allocated": 0.0,
            "occurrence_ids": [],
        },
    )
    substitute = context.entities.create(
        InventoryLot,
        key=("warehouse-reference", "lot-b"),
        state="available",
        attributes={
            "lot_key": "lot-b",
            "sku": SUBSTITUTE_SKU,
            "on_hand": substitute_on_hand,
            "allocated": 0.0,
            "occurrence_ids": [],
        },
    )
    with persistence.transaction() as uow:
        for entity in (order, primary, substitute):
            uow.save_entity(entity)
        uow.save_resource_definition(
            ResourceDefinition(PICKER_RESOURCE, capacity=picker_capacity)
        )
        uow.save_resource_definition(
            ResourceDefinition(PACKING_RESOURCE, capacity=packing_station_capacity)
        )
        uow.save_resource_definition(
            ResourceDefinition(SHIPPING_RESOURCE, capacity=shipping_dock_capacity)
        )
    return WarehouseEntities(
        order_id=order.id,
        lot_ids=(primary.id, substitute.id),
    )


def _entity(persistence, entity_type: str, entity_id: str):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _order_entity(
    persistence: MemoryPersistence,
    entities: WarehouseEntities,
) -> FulfillmentOrder:
    return _entity(
        persistence,
        "warehouse_fulfillment_order",
        entities.order_id,
    )


def _lot_entity(persistence: MemoryPersistence, lot_id: str) -> InventoryLot:
    return _entity(persistence, "warehouse_inventory_lot", lot_id)


def _allocation_entity(
    persistence: MemoryPersistence,
    allocation_id_value: str,
) -> Allocation:
    return _entity(
        persistence,
        "warehouse_allocation",
        allocation_id_value,
    )


def _dispatch(engine: Engine, entity, event: str, *, key: tuple[object, ...]) -> None:
    order_id = str(
        entity.attributes.get("order_id")
        or entity.attributes.get("fulfillment_order_id")
        or entity.id
    )
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=flow_correlation_id(order_id),
        key=key,
    )
    engine.dispatch(command)


def _available_quantity(lot: InventoryLot) -> float:
    return max(
        0.0,
        float(lot.attributes["on_hand"]) - float(lot.attributes["allocated"]),
    )


def _resolve_existing_allocations(
    persistence: MemoryPersistence,
    order: FulfillmentOrder,
) -> bool:
    existing_ids = list(order.attributes.get("allocation_ids", []))
    if not existing_ids:
        return False
    if all(
        persistence.entity("warehouse_allocation", str(aid)) is not None
        for aid in existing_ids
    ):
        return True
    raise RuntimeError("allocation index references missing durable allocation")


def _eligible_lots_for_order(
    persistence: MemoryPersistence,
    *,
    entities: WarehouseEntities,
    requested_sku: str,
    acceptable: tuple[str, ...],
) -> list[InventoryLot]:
    lots = [_lot_entity(persistence, lot_id) for lot_id in entities.lot_ids]
    eligible = [
        lot
        for lot in lots
        if str(lot.attributes["sku"]) in acceptable
        and _available_quantity(lot) > 0
    ]
    eligible.sort(
        key=lambda lot: (
            0 if lot.attributes["sku"] == requested_sku else 1,
            lot.id,
        )
    )
    return eligible


def _build_allocation_plan(
    eligible_lots: list[InventoryLot],
    *,
    needed: float,
) -> list[tuple[InventoryLot, float]] | None:
    plan: list[tuple[InventoryLot, float]] = []
    remaining = needed
    for lot in eligible_lots:
        quantity = min(remaining, _available_quantity(lot))
        if quantity > 0:
            plan.append((lot, quantity))
            remaining -= quantity
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    return plan


def _materialize_allocations(
    engine: Engine,
    order: FulfillmentOrder,
    plan: list[tuple[InventoryLot, float]],
    *,
    requested_sku: str,
) -> list[Allocation]:
    allocations: list[Allocation] = []
    for lot, quantity in plan:
        allocation = engine.context.entities.create(
            Allocation,
            key=("warehouse-reference", order.id, lot.id),
            state="committed",
            attributes={
                "order_id": order.id,
                "lot_id": lot.id,
                "requested_sku": requested_sku,
                "supplied_sku": lot.attributes["sku"],
                "quantity": quantity,
                "substituted": lot.attributes["sku"] != requested_sku,
            },
        )
        lot.attributes["allocated"] = (
            float(lot.attributes["allocated"]) + quantity
        )
        allocations.append(allocation)
    return allocations


def allocate_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
) -> bool:
    order = _order_entity(persistence, entities)
    if order.state in {"allocated", "picking", "packed", "shipped"}:
        return True
    if order.state != "requested":
        return False

    if _resolve_existing_allocations(persistence, order):
        _dispatch(
            engine,
            order,
            "allocate",
            key=("warehouse-order", order.id, "allocate"),
        )
        return True

    requested_sku = str(order.attributes["requested_sku"])
    acceptable = tuple(str(v) for v in order.attributes["acceptable_skus"])
    needed = float(order.attributes["requested_quantity"])
    eligible = _eligible_lots_for_order(
        persistence,
        entities=entities,
        requested_sku=requested_sku,
        acceptable=acceptable,
    )
    plan = _build_allocation_plan(eligible, needed=needed)
    if plan is None:
        return False

    allocations = _materialize_allocations(
        engine,
        order,
        plan,
        requested_sku=requested_sku,
    )

    order.attributes["allocation_ids"] = [
        allocation.id for allocation in allocations
    ]
    with persistence.transaction() as uow:
        uow.save_entity(order)
        for lot, _ in plan:
            uow.save_entity(lot)
        for allocation in allocations:
            uow.save_entity(allocation)

    order = _order_entity(persistence, entities)
    _dispatch(
        engine,
        order,
        "allocate",
        key=("warehouse-order", order.id, "allocate"),
    )
    return True


def _ensure_occurrence(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    kind: str,
    subject_id: str,
    sequence: int,
    attributes: dict[str, object],
    owner=None,
    lot: InventoryLot | None = None,
) -> InventoryOccurrence:
    oid = occurrence_id(kind, subject_id, sequence)
    existing = persistence.entity("warehouse_inventory_occurrence", oid)
    if existing is not None:
        _validate_occurrence_identity(existing, attributes)
    else:
        occurrence = engine.context.entities.create(
            InventoryOccurrence,
            key=("warehouse-reference", kind, subject_id, sequence),
            state="captured",
            attributes={
                "kind": kind,
                "subject_id": subject_id,
                "sequence": sequence,
                **attributes,
            },
        )
        _append_occurrence_id(owner, occurrence.id)
        _append_occurrence_id(lot, occurrence.id)
        _persist_occurrence_scope(
            persistence,
            occurrence=occurrence,
            owner=owner,
            lot=lot,
        )

    occurrence = _entity(
        persistence,
        "warehouse_inventory_occurrence",
        oid,
    )
    if occurrence.state == "captured":
        _dispatch(
            engine,
            occurrence,
            "commit",
            key=("warehouse-occurrence", occurrence.id, "commit"),
        )
    return _entity(
        persistence,
        "warehouse_inventory_occurrence",
        oid,
    )


def _validate_occurrence_identity(
    occurrence: InventoryOccurrence,
    attributes: dict[str, object],
) -> None:
    for key, value in attributes.items():
        if occurrence.attributes.get(key) != value:
            raise ValueError(
                "occurrence identity already exists with different evidence"
            )


def _append_occurrence_id(entity, occurrence_id_value: str) -> None:
    if entity is None:
        return
    occurrence_ids = list(entity.attributes.get("occurrence_ids", []))
    if occurrence_id_value in occurrence_ids:
        return
    occurrence_ids.append(occurrence_id_value)
    entity.attributes["occurrence_ids"] = occurrence_ids


def _persist_occurrence_scope(
    persistence: MemoryPersistence,
    *,
    occurrence: InventoryOccurrence,
    owner,
    lot: InventoryLot | None,
) -> None:
    with persistence.transaction() as uow:
        uow.save_entity(occurrence)
        if owner is not None:
            uow.save_entity(owner)
        if lot is not None:
            uow.save_entity(lot)


def pick_allocation(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
    allocation_id_value: str,
) -> Allocation:
    order = _order_entity(persistence, entities)
    allocation = _allocation_entity(persistence, allocation_id_value)
    if order.state == "allocated":
        _dispatch(
            engine,
            order,
            "start_pick",
            key=("warehouse-order", order.id, "start-pick"),
        )
        order = _order_entity(persistence, entities)
    if order.state != "picking":
        raise RuntimeError("picking requires allocated fulfillment order")
    if allocation.state in {"picked", "shipped"}:
        return allocation

    lot = _lot_entity(persistence, str(allocation.attributes["lot_id"]))
    quantity = float(allocation.attributes["quantity"])
    oid = occurrence_id("pick", allocation.id, 1)
    occurrence = persistence.entity("warehouse_inventory_occurrence", oid)

    if occurrence is None:
        if float(lot.attributes["allocated"]) + 1e-9 < quantity:
            raise RuntimeError("lot allocation projection is below committed quantity")
        if float(lot.attributes["on_hand"]) + 1e-9 < quantity:
            raise RuntimeError("lot on-hand projection is below committed quantity")
        lot.attributes["on_hand"] = round(
            float(lot.attributes["on_hand"]) - quantity,
            6,
        )
        lot.attributes["allocated"] = round(
            float(lot.attributes["allocated"]) - quantity,
            6,
        )
        occurrence = engine.context.entities.create(
            InventoryOccurrence,
            key=("warehouse-reference", "pick", allocation.id, 1),
            state="captured",
            attributes={
                "kind": "pick",
                "subject_id": allocation.id,
                "sequence": 1,
                "order_id": order.id,
                "allocation_id": allocation.id,
                "lot_id": lot.id,
                "sku": allocation.attributes["supplied_sku"],
                "quantity": quantity,
                "delta_on_hand": -quantity,
                "resulting_on_hand": lot.attributes["on_hand"],
            },
        )
        order_occurrences = list(order.attributes.get("occurrence_ids", []))
        order_occurrences.append(occurrence.id)
        order.attributes["occurrence_ids"] = order_occurrences
        lot_occurrences = list(lot.attributes.get("occurrence_ids", []))
        lot_occurrences.append(occurrence.id)
        lot.attributes["occurrence_ids"] = lot_occurrences
        with persistence.transaction() as uow:
            uow.save_entity(lot)
            uow.save_entity(order)
            uow.save_entity(occurrence)

    occurrence = _entity(
        persistence,
        "warehouse_inventory_occurrence",
        oid,
    )
    if occurrence.state == "captured":
        _dispatch(
            engine,
            occurrence,
            "commit",
            key=("warehouse-occurrence", occurrence.id, "commit"),
        )

    allocation = _allocation_entity(persistence, allocation.id)
    if allocation.state == "committed":
        _dispatch(
            engine,
            allocation,
            "pick",
            key=("warehouse-allocation", allocation.id, "pick"),
        )
    return _allocation_entity(persistence, allocation.id)


def pick_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
) -> bool:
    order = _order_entity(persistence, entities)
    if order.state == "shipped":
        return True
    if order.state not in {"allocated", "picking"}:
        raise RuntimeError("pick requires allocated fulfillment order")
    for allocation_id_value in order.attributes.get("allocation_ids", []):
        pick_allocation(
            persistence,
            engine,
            entities=entities,
            allocation_id_value=str(allocation_id_value),
        )
    return True


def pack_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
) -> bool:
    order = _order_entity(persistence, entities)
    if order.state == "packed":
        return True
    if order.state != "picking":
        raise RuntimeError("pack requires picking fulfillment order")
    allocations = [
        _allocation_entity(persistence, str(aid))
        for aid in order.attributes.get("allocation_ids", [])
    ]
    if not allocations or any(a.state != "picked" for a in allocations):
        raise RuntimeError("pack requires all allocations to be picked")

    _ensure_occurrence(
        persistence,
        engine,
        kind="pack",
        subject_id=order.id,
        sequence=1,
        attributes={
            "order_id": order.id,
            "allocation_ids": [a.id for a in allocations],
        },
        owner=order,
    )
    order = _order_entity(persistence, entities)
    _dispatch(
        engine,
        order,
        "pack",
        key=("warehouse-order", order.id, "pack"),
    )
    return True


def ship_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
) -> bool:
    order = _order_entity(persistence, entities)
    if order.state == "shipped":
        return True
    if order.state != "packed":
        raise RuntimeError("ship requires packed fulfillment order")
    allocations = [
        _allocation_entity(persistence, str(aid))
        for aid in order.attributes.get("allocation_ids", [])
    ]
    for allocation in allocations:
        if allocation.state == "picked":
            _dispatch(
                engine,
                allocation,
                "ship",
                key=("warehouse-allocation", allocation.id, "ship"),
            )

    _ensure_occurrence(
        persistence,
        engine,
        kind="ship",
        subject_id=order.id,
        sequence=1,
        attributes={
            "order_id": order.id,
            "allocation_ids": [a.id for a in allocations],
        },
        owner=_order_entity(persistence, entities),
    )
    order = _order_entity(persistence, entities)
    _dispatch(
        engine,
        order,
        "ship",
        key=("warehouse-order", order.id, "ship"),
    )
    return True


def _service_task_entity(
    persistence: MemoryPersistence,
    *,
    stage: str,
    subject_id: str,
) -> FulfillmentServiceTask | None:
    return persistence.entity(
        "warehouse_fulfillment_service_task",
        service_task_id(stage, subject_id),
    )


def _save_entity(persistence: MemoryPersistence, entity) -> None:
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def _ensure_service_task(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    stage: str,
    subject_id: str,
    order_id: str,
    resource_name: str,
    service_duration: timedelta,
) -> FulfillmentServiceTask:
    if service_duration <= timedelta(0):
        raise ValueError("service duration must be positive")

    existing = _service_task_entity(
        persistence,
        stage=stage,
        subject_id=subject_id,
    )
    duration_seconds = service_duration.total_seconds()
    if existing is not None:
        if existing.attributes.get("resource_name") != resource_name:
            raise ValueError("service task resource identity changed")
        if float(existing.attributes.get("service_duration_seconds", 0.0)) != duration_seconds:
            raise ValueError("service task duration changed after creation")
        return existing

    task = engine.context.entities.create(
        FulfillmentServiceTask,
        key=("warehouse-reference", "service", stage, subject_id),
        state="queued",
        attributes={
            "stage": stage,
            "subject_id": subject_id,
            "order_id": order_id,
            "resource_name": resource_name,
            "request_id": f"warehouse-fulfillment:{stage}:{subject_id}",
            "requested_at": engine.context.clock.now.isoformat(),
            "acquired_at": None,
            "service_duration_seconds": duration_seconds,
            "completion_due_at": None,
            "reservation_id": None,
            "business_applied": False,
            "resource_released": False,
        },
    )
    _save_entity(persistence, task)
    return task


def _start_service_task_if_capacity_available(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    *,
    task: FulfillmentServiceTask,
) -> FulfillmentServiceTask:
    if task.state == "completed":
        return task

    requested_at = datetime.fromisoformat(str(task.attributes["requested_at"]))
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name=str(task.attributes["resource_name"]),
        request_id=str(task.attributes["request_id"]),
        requested_at=requested_at,
    )
    task = _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )
    if reservation is None:
        return task

    if task.attributes.get("acquired_at") is None:
        acquired_at = reservation.acquired_at
        duration = timedelta(
            seconds=float(task.attributes["service_duration_seconds"])
        )
        task.attributes["acquired_at"] = acquired_at.isoformat()
        task.attributes["completion_due_at"] = (acquired_at + duration).isoformat()
        task.attributes["reservation_id"] = reservation.reservation_id
        _save_entity(persistence, task)

    task = _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )
    if task.state == "queued":
        _dispatch(
            engine,
            task,
            "start",
            key=("warehouse-service", task.id, "start"),
        )
        task = _entity(
            persistence,
            "warehouse_fulfillment_service_task",
            task.id,
        )

    pending = engine.scheduler.find_pending(
        entity_type=task.entity_type,
        entity_id=task.id,
        name="complete",
    )
    if task.state == "in_progress" and pending is None:
        due_at = datetime.fromisoformat(str(task.attributes["completion_due_at"]))
        command = engine.context.commands.create(
            "complete",
            target=task,
            due_at=due_at,
            correlation_id=flow_correlation_id(str(task.attributes["order_id"])),
            key=("warehouse-service", task.id, "complete"),
        )
        engine.context.schedules.at(due_at, command=command)

    return _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )


def _release_service_capacity(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    task: FulfillmentServiceTask,
) -> None:
    if bool(task.attributes.get("resource_released", False)):
        return

    request_id = str(task.attributes["request_id"])
    engine.resources.withdraw(backend, request_id)

    task = _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )
    task.attributes["resource_released"] = True
    _save_entity(persistence, task)


def _apply_completed_service_task(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    *,
    entities: WarehouseEntities,
    task: FulfillmentServiceTask,
) -> None:
    if task.state != "completed":
        return

    if not bool(task.attributes.get("business_applied", False)):
        stage = str(task.attributes["stage"])
        subject_id = str(task.attributes["subject_id"])
        if stage == "pick":
            pick_allocation(
                persistence,
                engine,
                entities=entities,
                allocation_id_value=subject_id,
            )
        elif stage == "pack":
            pack_order(persistence, engine, entities=entities)
        elif stage == "ship":
            ship_order(persistence, engine, entities=entities)
        else:
            raise ValueError(f"unknown fulfillment service stage: {stage}")

        task = _entity(
            persistence,
            "warehouse_fulfillment_service_task",
            task.id,
        )
        task.attributes["business_applied"] = True
        _save_entity(persistence, task)

    task = _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )
    _release_service_capacity(persistence, engine, backend, task)


def _reconcile_service_task(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    *,
    entities: WarehouseEntities,
    stage: str,
    subject_id: str,
    order_id: str,
    resource_name: str,
    service_duration: timedelta,
) -> FulfillmentServiceTask:
    task = _ensure_service_task(
        persistence,
        engine,
        stage=stage,
        subject_id=subject_id,
        order_id=order_id,
        resource_name=resource_name,
        service_duration=service_duration,
    )
    if task.state == "completed":
        _apply_completed_service_task(
            persistence,
            engine,
            backend,
            entities=entities,
            task=task,
        )
        return _entity(
            persistence,
            "warehouse_fulfillment_service_task",
            task.id,
        )

    return _start_service_task_if_capacity_available(
        persistence,
        engine,
        backend,
        task=task,
    )


def reconcile_fulfillment_services(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    *,
    entities: WarehouseEntities,
    pick_duration: timedelta,
    pack_duration: timedelta,
    ship_duration: timedelta,
) -> None:
    """Advance finite-capacity pick/pack/ship work without early business effects."""

    order = _order_entity(persistence, entities)
    if order.state == "shipped":
        return
    if order.state not in {"allocated", "picking", "packed"}:
        raise RuntimeError("service reconciliation requires allocated fulfillment order")

    if order.state == "allocated":
        _dispatch(
            engine,
            order,
            "start_pick",
            key=("warehouse-order", order.id, "start-pick"),
        )
        order = _order_entity(persistence, entities)

    if order.state == "picking":
        allocation_ids = tuple(
            str(value) for value in order.attributes.get("allocation_ids", [])
        )
        for allocation_id_value in allocation_ids:
            allocation = _allocation_entity(persistence, allocation_id_value)
            task = _service_task_entity(
                persistence,
                stage="pick",
                subject_id=allocation_id_value,
            )
            if task is not None and task.state == "completed":
                _apply_completed_service_task(
                    persistence,
                    engine,
                    backend,
                    entities=entities,
                    task=task,
                )
                allocation = _allocation_entity(persistence, allocation_id_value)
            if allocation.state == "committed":
                _reconcile_service_task(
                    persistence,
                    engine,
                    backend,
                    entities=entities,
                    stage="pick",
                    subject_id=allocation.id,
                    order_id=order.id,
                    resource_name=PICKER_RESOURCE,
                    service_duration=pick_duration,
                )

        allocations = [
            _allocation_entity(persistence, allocation_id_value)
            for allocation_id_value in allocation_ids
        ]
        if allocations and all(allocation.state == "picked" for allocation in allocations):
            _reconcile_service_task(
                persistence,
                engine,
                backend,
                entities=entities,
                stage="pack",
                subject_id=order.id,
                order_id=order.id,
                resource_name=PACKING_RESOURCE,
                service_duration=pack_duration,
            )

    order = _order_entity(persistence, entities)
    pack_task = _service_task_entity(
        persistence,
        stage="pack",
        subject_id=order.id,
    )
    if pack_task is not None and pack_task.state == "completed":
        _apply_completed_service_task(
            persistence,
            engine,
            backend,
            entities=entities,
            task=pack_task,
        )
        order = _order_entity(persistence, entities)

    if order.state == "packed":
        _reconcile_service_task(
            persistence,
            engine,
            backend,
            entities=entities,
            stage="ship",
            subject_id=order.id,
            order_id=order.id,
            resource_name=SHIPPING_RESOURCE,
            service_duration=ship_duration,
        )

    ship_task = _service_task_entity(
        persistence,
        stage="ship",
        subject_id=order.id,
    )
    if ship_task is not None and ship_task.state == "completed":
        _apply_completed_service_task(
            persistence,
            engine,
            backend,
            entities=entities,
            task=ship_task,
        )


def correct_lot_balance(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    lot_id: str,
    sequence: int,
    delta: float,
) -> InventoryOccurrence:
    if sequence <= 0:
        raise ValueError("correction sequence must be positive")
    oid = occurrence_id("correction", lot_id, sequence)
    existing = persistence.entity("warehouse_inventory_occurrence", oid)
    if existing is not None:
        if float(existing.attributes["delta_on_hand"]) != float(delta):
            raise ValueError(
                "occurrence identity already exists with different evidence"
            )
        if existing.state == "captured":
            _dispatch(
                engine,
                existing,
                "commit",
                key=("warehouse-occurrence", existing.id, "commit"),
            )
        return _entity(
            persistence,
            "warehouse_inventory_occurrence",
            oid,
        )

    lot = _lot_entity(persistence, lot_id)
    next_on_hand = round(float(lot.attributes["on_hand"]) + float(delta), 6)
    if next_on_hand < float(lot.attributes["allocated"]) - 1e-9:
        raise ValueError("correction cannot reduce on-hand below allocated quantity")
    if next_on_hand < -1e-9:
        raise ValueError("correction cannot make on-hand negative")

    lot.attributes["on_hand"] = next_on_hand
    occurrence = engine.context.entities.create(
        InventoryOccurrence,
        key=("warehouse-reference", "correction", lot.id, sequence),
        state="captured",
        attributes={
            "kind": "correction",
            "subject_id": lot.id,
            "sequence": sequence,
            "lot_id": lot.id,
            "sku": lot.attributes["sku"],
            "delta_on_hand": float(delta),
            "resulting_on_hand": next_on_hand,
        },
    )
    occurrence_ids = list(lot.attributes.get("occurrence_ids", []))
    occurrence_ids.append(occurrence.id)
    lot.attributes["occurrence_ids"] = occurrence_ids
    with persistence.transaction() as uow:
        uow.save_entity(lot)
        uow.save_entity(occurrence)

    _dispatch(
        engine,
        occurrence,
        "commit",
        key=("warehouse-occurrence", occurrence.id, "commit"),
    )
    return _entity(
        persistence,
        "warehouse_inventory_occurrence",
        occurrence.id,
    )


def run_happy_path() -> tuple[MemoryPersistence, WarehouseEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    if not allocate_order(persistence, engine, entities=entities):
        raise RuntimeError("reference inventory could not satisfy order")
    pick_order(persistence, engine, entities=entities)
    pack_order(persistence, engine, entities=entities)
    ship_order(persistence, engine, entities=entities)
    return persistence, entities
