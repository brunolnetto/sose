"""Aviation constants, ids, runtime wiring and entity helpers (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import CounterRandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .scenarios import ORIGIN
from .statecharts import (
    AircraftChart,
    CrewAssignmentChart,
    FlightChart,
    InspectionChart,
    MaintenanceWorkOrderChart,
    PartDemandChart,
)

LEG1_DEPARTURE_DELAY = timedelta(hours=1)


LEG2_DEPARTURE_DELAY = timedelta(hours=5)


def flow_correlation_id(aircraft_id: str) -> str:
    return deterministic_id("aviation-rotation-flow", aircraft_id)


def inspection_id(flight_id: str) -> str:
    return deterministic_id(
        "entity",
        "aviation_inspection",
        "aviation-reference",
        flight_id,
        "inspection",
    )


def maintenance_work_order_id(flight_id: str) -> str:
    return deterministic_id(
        "entity",
        "aviation_maintenance_work_order",
        "aviation-reference",
        flight_id,
        "maintenance",
    )


def part_demand_id(flight_id: str) -> str:
    return deterministic_id(
        "entity",
        "aviation_part_demand",
        "aviation-reference",
        flight_id,
        "part-demand",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 713,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=CounterRandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("aviation_aircraft", AircraftChart))
    registry.register(EntityType("aviation_flight", FlightChart))
    registry.register(EntityType("aviation_crew_assignment", CrewAssignmentChart))
    registry.register(EntityType("aviation_inspection", InspectionChart))
    registry.register(
        EntityType(
            "aviation_maintenance_work_order",
            MaintenanceWorkOrderChart,
        )
    )
    registry.register(EntityType("aviation_part_demand", PartDemandChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def _entity(persistence, entity_type, entity_id):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _aircraft(persistence, entities):
    return _entity(persistence, "aviation_aircraft", entities.aircraft_id)


def _flight(persistence, flight_id):
    return _entity(persistence, "aviation_flight", flight_id)


def _crew(persistence, crew_id):
    return _entity(persistence, "aviation_crew_assignment", crew_id)


def _dispatch(engine, entity, event, *, key, correlation_id):
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)
