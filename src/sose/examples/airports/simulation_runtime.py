"""Airport constants, flow ids, runtime wiring and entity helpers (split from simulation.py)."""

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
    BaggageFlowChart,
    DepartureSlotChart,
    FlightTurnaroundChart,
    GateAssignmentChart,
    GroundServiceTaskChart,
)

ARRIVAL_DELAY = timedelta(hours=1)
DEPARTURE_SLOT_DELAY = timedelta(hours=4)


def flow_correlation_id(turnaround_id: str) -> str:
    return deterministic_id("airport-turnaround-flow", turnaround_id)


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 612,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=CounterRandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("airport_flight_turnaround", FlightTurnaroundChart))
    registry.register(EntityType("airport_gate_assignment", GateAssignmentChart))
    registry.register(EntityType("airport_ground_service_task", GroundServiceTaskChart))
    registry.register(EntityType("airport_baggage_flow", BaggageFlowChart))
    registry.register(EntityType("airport_departure_slot", DepartureSlotChart))
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


def _turnaround(persistence, entities):
    return _entity(
        persistence,
        "airport_flight_turnaround",
        entities.turnaround_id,
    )


def _gate_assignment(persistence, entities):
    return _entity(
        persistence,
        "airport_gate_assignment",
        entities.gate_assignment_id,
    )


def _service_task(persistence, entities):
    return _entity(
        persistence,
        "airport_ground_service_task",
        entities.service_task_id,
    )


def _baggage(persistence, entities):
    return _entity(
        persistence,
        "airport_baggage_flow",
        entities.baggage_flow_id,
    )


def _slot(persistence, entities):
    return _entity(
        persistence,
        "airport_departure_slot",
        entities.departure_slot_id,
    )


def _dispatch(engine, entity, event, *, key, correlation_id):
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)
