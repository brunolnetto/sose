"""ITSM constants, ids, runtime setup and lookup helpers (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import Escalation, Incident
from .scenarios import ORIGIN
from .statecharts import EscalationChart, IncidentChart


SLA_DELAY = timedelta(hours=4)


@dataclass(frozen=True, slots=True)
class ITSMEntities:
    incident_id: str


def flow_correlation_id(incident_id: str) -> str:
    return deterministic_id("itsm-flow", incident_id)


def escalation_id(incident_id: str) -> str:
    return deterministic_id(
        "entity",
        "itsm_escalation",
        "itsm-reference",
        incident_id,
        "escalation-1",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 210,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("itsm_incident", IncidentChart))
    registry.register(EntityType("itsm_escalation", EscalationChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def create_incident(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    key: str,
    severity: int,
) -> Incident:
    incident = engine.context.entities.create(
        Incident,
        key=("itsm-reference", key),
        state="opened",
        attributes={"severity": severity, "service": "payments-api"},
    )
    with persistence.transaction() as uow:
        uow.save_entity(incident)
    return incident


def _incident(persistence: MemoryPersistence, incident_id: str) -> Incident:
    incident = persistence.entity("itsm_incident", incident_id)
    if incident is None:
        raise RuntimeError(f"incident was not persisted: {incident_id}")
    return incident


def _escalation(
    persistence: MemoryPersistence,
    incident_id: str,
) -> Escalation | None:
    return persistence.entity("itsm_escalation", escalation_id(incident_id))


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


def cancel_sla(
    engine: Engine,
    incident_id: str,
) -> bool:
    return engine.scheduler.cancel_pending(
        entity_type="itsm_incident",
        entity_id=incident_id,
        name="escalate",
    )


def release_incident_owner(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    claim_id: str,
) -> None:
    engine.resources.withdraw(backend, f"support-agent:{claim_id}")
