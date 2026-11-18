from __future__ import annotations

from datetime import datetime, timedelta

from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import (  # noqa: F401 - historical import location
    Allocation,
    FulfillmentOrder,
    FulfillmentServiceTask,
    InventoryLot,
    InventoryOccurrence,
)
from .scenarios import ORIGIN
from .statecharts import (  # noqa: F401 - historical import location
    AllocationChart,
    FulfillmentOrderChart,
    FulfillmentServiceTaskChart,
    InventoryOccurrenceChart,
)
from .records import (  # noqa: F401 - historical import location
    PACKING_RESOURCE,
    PICKER_RESOURCE,
    PRIMARY_SKU,
    REGISTERED_ENTITY_TYPES,
    SHIPPING_RESOURCE,
    SUBSTITUTE_SKU,
    WarehouseEntities,
    _allocation_entity,
    _dispatch,
    _order_entity,
    build_runtime,
    flow_correlation_id,
    occurrence_id,
)
from .allocation import (  # noqa: F401 - historical import location
    allocate_order,
)
from .handling import (  # noqa: F401 - historical import location
    _append_occurrence_id,
    _validate_occurrence_identity,
    pack_order,
    pick_order,
    ship_order,
)
from .composed import (  # noqa: F401 - historical import location
    allocate_composed_order,
    pick_composed_order,
    seed_composed_reference,
)
from .service_tasks import (  # noqa: F401 - historical import location
    _apply_completed_service_task,
    _ensure_service_task,
    _reconcile_service_task,
    _service_task_entity,
)


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    requested_quantity: float = 10.0,
    primary_on_hand: float = 6.0,
    substitute_on_hand: float = 5.0,
    allow_substitute: bool = True,
    picker_capacity: int = 1,
    packing_station_capacity: int = 1,
    shipping_dock_capacity: int = 1,
) -> WarehouseEntities:
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
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


def _reconcile_picking_services(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    *,
    entities: WarehouseEntities,
    order: FulfillmentOrder,
    pick_duration: timedelta,
    pack_duration: timedelta,
) -> None:
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
        _reconcile_picking_services(
            persistence,
            engine,
            backend,
            entities=entities,
            order=order,
            pick_duration=pick_duration,
            pack_duration=pack_duration,
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


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, WarehouseEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)

    if not allocate_order(persistence, engine, entities=entities):
        raise RuntimeError("reference inventory could not satisfy order")
    pick_order(persistence, engine, entities=entities)
    pack_order(persistence, engine, entities=entities)
    ship_order(persistence, engine, entities=entities)
    return persistence, entities


# Moved by the quality-ratchet splits (#632); importing these names from here
# is deprecated (ADR-0011). Import them from the module named on the right.
from sose._deprecation import deprecated_reexports as _deprecated_reexports

__getattr__ = _deprecated_reexports(__name__, {
    "allocation_id": "sose.examples.warehouse_fulfillment.records",
    "correct_lot_balance": "sose.examples.warehouse_fulfillment.handling",
    "deterministic_id": "sose.core.identity",
    "pick_allocation": "sose.examples.warehouse_fulfillment.handling",
    "service_task_id": "sose.examples.warehouse_fulfillment.records",
}, since="0.8.1", removed_in="0.9.0")
