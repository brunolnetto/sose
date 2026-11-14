"""Energy constants, ids, runtime wiring and entity/dispatch helpers (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import (
    Meter,
    ServicePoint,
)
from .scenarios import ORIGIN
from .statecharts import (
    DemandResponseEventChart,
    DemandResponseParticipationChart,
    MeterChart,
    MeterReadingChart,
    OutageChart,
    ServicePointChart,
)

if TYPE_CHECKING:
    from .simulation import EnergyEntities


DR_START_DELAY = timedelta(hours=1)
DR_DURATION = timedelta(hours=3)


def flow_correlation_id(service_point_id: str) -> str:
    return deterministic_id("energy-utilities-flow", service_point_id)


def reading_id(
    meter_id: str,
    interval_end: datetime,
    *,
    correction_ordinal: int = 0,
) -> str:
    return deterministic_id(
        "entity",
        "utility_meter_reading",
        "energy-reference",
        meter_id,
        interval_end.isoformat(),
        correction_ordinal,
    )


def outage_id(service_point_id: str, incident_key: str) -> str:
    return deterministic_id(
        "entity",
        "utility_outage",
        "energy-reference",
        service_point_id,
        "outage",
        incident_key,
    )


def dr_event_id(event_key: str) -> str:
    return deterministic_id(
        "entity",
        "utility_dr_event",
        "energy-reference",
        "dr-event",
        event_key,
    )


def dr_participation_id(event_id: str, service_point_id: str) -> str:
    return deterministic_id(
        "entity",
        "utility_dr_participation",
        "energy-reference",
        event_id,
        service_point_id,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 1009,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("utility_service_point", ServicePointChart))
    registry.register(EntityType("utility_meter", MeterChart))
    registry.register(EntityType("utility_meter_reading", MeterReadingChart))
    registry.register(EntityType("utility_outage", OutageChart))
    registry.register(EntityType("utility_dr_event", DemandResponseEventChart))
    registry.register(
        EntityType(
            "utility_dr_participation",
            DemandResponseParticipationChart,
        )
    )
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


def _service_point(
    persistence: MemoryPersistence,
    entities: EnergyEntities,
) -> ServicePoint:
    return _entity(
        persistence,
        "utility_service_point",
        entities.service_point_id,
    )


def _meter(persistence: MemoryPersistence, entities: EnergyEntities) -> Meter:
    return _entity(persistence, "utility_meter", entities.meter_id)


def _dispatch(
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
