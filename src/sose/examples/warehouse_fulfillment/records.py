"""Warehouse fulfillment ids, runtime builder and entity lookups (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import (
    Allocation,
    FulfillmentOrder,
    InventoryLot,
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

REGISTERED_ENTITY_TYPES = (
    EntityType("warehouse_fulfillment_order", FulfillmentOrderChart),
    EntityType("warehouse_allocation", AllocationChart),
    EntityType("warehouse_inventory_occurrence", InventoryOccurrenceChart),
    EntityType("warehouse_fulfillment_service_task", FulfillmentServiceTaskChart),
)


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
    owner_id: str,
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
    for entity_type in REGISTERED_ENTITY_TYPES:
        registry.register(entity_type)
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
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
