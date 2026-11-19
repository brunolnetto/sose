"""Warehouse management ids, runtime builder and entity helpers (split from simulation.py)."""

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

from .statecharts import DockChart, ForkliftChart, ShipmentChart, TruckChart, WarehouseSiteChart


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
REFERENCE_SKU = "widget-a"

REGISTERED_ENTITY_TYPES = (
    EntityType("warehouse_management_site", WarehouseSiteChart),
    EntityType("warehouse_management_dock", DockChart),
    EntityType("warehouse_management_truck", TruckChart),
    EntityType("warehouse_management_forklift", ForkliftChart),
    EntityType("warehouse_management_shipment", ShipmentChart),
)


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
    owner_id: str,
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
    for entity_type in REGISTERED_ENTITY_TYPES:
        registry.register(entity_type)
    return context, Engine(
        context=context, registry=registry, persistence=persistence, owner_id=owner_id
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
