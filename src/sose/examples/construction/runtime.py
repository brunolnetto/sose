from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ContainerDefinition, ResourceDefinition, StoreDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import ConstructionActivity, ConstructionInspection, ConstructionMeasurement
from .scenarios import ORIGIN
from .statecharts import ActivityChart, InspectionChart


MATERIAL_CAPACITY = 1_000.0


@dataclass(frozen=True, slots=True)
class ConstructionEntities:
    predecessor_id: str
    activity_id: str


def flow_correlation_id(activity_id: str) -> str:
    return deterministic_id("construction-flow", activity_id)


def inspection_id(activity_id: str, ordinal: int) -> str:
    if ordinal < 1:
        raise ValueError("inspection ordinal must be >= 1")
    return deterministic_id(
        "entity",
        "construction_inspection",
        "construction-reference",
        activity_id,
        "inspection",
        ordinal,
    )




def measurement_id(activity_id: str) -> str:
    return deterministic_id(
        "entity",
        "construction_measurement",
        "construction-reference",
        activity_id,
        "measurement",
    )

def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=294),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("construction_activity", ActivityChart))
    registry.register(EntityType("construction_inspection", InspectionChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    quantity: float = 10.0,
    predecessor_completed: bool = True,
) -> ConstructionEntities:
    if quantity <= 0 or quantity > MATERIAL_CAPACITY:
        raise ValueError("quantity must fit construction material capacity")

    context, engine = build_runtime(persistence)
    predecessor = context.entities.create(
        ConstructionActivity,
        key=("construction-reference", "predecessor"),
        state="completed" if predecessor_completed else "measured",
        attributes={"name": "foundation", "measurement": 1.0},
    )
    activity = context.entities.create(
        ConstructionActivity,
        key=("construction-reference", "activity-1"),
        state="planned",
        attributes={
            "name": "structural-frame",
            "predecessor_id": predecessor.id,
            "quantity": quantity,
            "material_staged": False,
        },
    )

    with persistence.transaction() as uow:
        uow.save_entity(predecessor)
        uow.save_entity(activity)
        for name in ("crew", "equipment", "inspector"):
            uow.save_resource_definition(ResourceDefinition(name, capacity=1))

    engine.stores.define(StoreDefinition("material_lots", kind="fifo", capacity=10))
    engine.containers.define(
        ContainerDefinition(
            "material_quantity",
            capacity=MATERIAL_CAPACITY,
            initial=0.0,
        )
    )
    return ConstructionEntities(predecessor.id, activity.id)


def activity(persistence: MemoryPersistence, activity_id: str) -> ConstructionActivity:
    value = persistence.entity("construction_activity", activity_id)
    if value is None:
        raise RuntimeError(f"construction activity was not persisted: {activity_id}")
    return value


def inspection(
    persistence: MemoryPersistence,
    activity_id: str,
    ordinal: int,
) -> ConstructionInspection | None:
    return persistence.entity(
        "construction_inspection",
        inspection_id(activity_id, ordinal),
    )


def dispatch(
    engine: Engine,
    entity,
    event: str,
    *,
    key: tuple[object, ...],
    correlation_id: str,
) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def save_activity_attributes(
    persistence: MemoryPersistence,
    entity: ConstructionActivity,
    **updates,
) -> ConstructionActivity:
    current = activity(persistence, entity.id)
    current.attributes.update(updates)
    with persistence.transaction() as uow:
        uow.save_entity(current)
    return activity(persistence, entity.id)


def resource_request_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        demand.request_id == request_id for demand in persistence.resource_demands()
    ) or any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )


def resource_reservation(persistence: MemoryPersistence, request_id: str):
    return next(
        (
            reservation
            for reservation in persistence.resource_reservations()
            if reservation.request_id == request_id
        ),
        None,
    )
