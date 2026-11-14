"""Transit constants, ids, runtime wiring and entity helpers (split from simulation.py)."""

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
    ScheduledTrip,
    Vehicle,
)
from .scenarios import ORIGIN
from .statecharts import (
    ScheduledTripChart,
    ServiceAlertChart,
    TripUpdateOccurrenceChart,
    VehicleChart,
    VehiclePositionOccurrenceChart,
)

if TYPE_CHECKING:
    from .simulation import TransitEntities


TRIP_A_START = ORIGIN + timedelta(hours=1)
TRIP_A_END = ORIGIN + timedelta(hours=2)
TRIP_B_START = ORIGIN + timedelta(hours=2, minutes=15)
TRIP_B_END = ORIGIN + timedelta(hours=3, minutes=15)
REALTIME_STALE_AFTER = timedelta(seconds=90)


def flow_correlation_id(block_id: str) -> str:
    return deterministic_id("transit-flow", block_id)


def trip_update_id(trip_id: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "transit_trip_update",
        "transit-reference",
        trip_id,
        "trip-update",
        sequence,
    )


def vehicle_position_id(vehicle_id: str, trip_id: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "transit_vehicle_position",
        "transit-reference",
        vehicle_id,
        trip_id,
        "position",
        sequence,
    )


def service_alert_id(alert_key: str) -> str:
    return deterministic_id(
        "entity",
        "transit_service_alert",
        "transit-reference",
        "alert",
        alert_key,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 1117,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("transit_vehicle", VehicleChart))
    registry.register(EntityType("transit_scheduled_trip", ScheduledTripChart))
    registry.register(EntityType("transit_trip_update", TripUpdateOccurrenceChart))
    registry.register(
        EntityType(
            "transit_vehicle_position",
            VehiclePositionOccurrenceChart,
        )
    )
    registry.register(EntityType("transit_service_alert", ServiceAlertChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def _entity(
    persistence: MemoryPersistence,
    entity_type: str,
    entity_id: str,
):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _vehicle(
    persistence: MemoryPersistence,
    entities: TransitEntities,
) -> Vehicle:
    return _entity(persistence, "transit_vehicle", entities.vehicle_id)


def _trip(
    persistence: MemoryPersistence,
    trip_id: str,
) -> ScheduledTrip:
    return _entity(persistence, "transit_scheduled_trip", trip_id)


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


def _at(value: object) -> datetime:
    return datetime.fromisoformat(str(value))
