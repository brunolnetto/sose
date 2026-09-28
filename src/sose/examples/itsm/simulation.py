from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition, StoreDefinition
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
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    severity: int = 50,
    service: str = "payments-api",
    support_agent_capacity: int = 1,
    escalation_manager_capacity: int = 1,
    incident_queue_capacity: int = 100,
) -> ITSMEntities:
    context, engine = build_runtime(persistence, now=now)
    incident = context.entities.create(
        Incident,
        key=("itsm-reference", "incident-1"),
        state="opened",
        attributes={"severity": severity, "service": service},
    )
    with persistence.transaction() as uow:
        uow.save_entity(incident)
        uow.save_resource_definition(ResourceDefinition("support_agent", capacity=support_agent_capacity))
        uow.save_resource_definition(
            ResourceDefinition("escalation_manager", capacity=escalation_manager_capacity)
        )
    engine.stores.define(
        StoreDefinition("incident_queue", kind="priority", capacity=incident_queue_capacity)
    )
    return ITSMEntities(incident_id=incident.id)


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


def triage_and_queue(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    incident_id: str,
    sla_delay: timedelta = SLA_DELAY,
) -> datetime:
    incident = _incident(persistence, incident_id)
    correlation_id = flow_correlation_id(incident.id)
    if incident.state == "opened":
        _dispatch(
            engine,
            incident,
            "triage",
            key=("itsm", incident.id, "triage"),
            correlation_id=correlation_id,
        )
        incident = _incident(persistence, incident.id)

    if incident.state != "triaged":
        raise RuntimeError(f"incident is not triaged: {incident.state}")

    item_id = f"incident-queue:{incident.id}"
    queued = any(
        item.item_id == item_id for item in persistence.store_items()
    )
    pending = any(
        intent.item_id == item_id for intent in persistence.store_put_intents()
    )
    consumed = any(
        result.item.item_id == item_id
        for result in persistence.store_get_results()
    )
    if not (queued or pending or consumed):
        engine.stores.put(
            backend,
            store_name="incident_queue",
            item_id=item_id,
            value={"incident_id": incident.id},
            priority=int(incident.attributes["severity"]),
            requested_at=backend.now,
        )
        backend.run_until(backend.now)

    existing = engine.scheduler.find_pending(
        entity_type="itsm_incident",
        entity_id=incident.id,
        name="escalate",
    )
    if existing is not None:
        return existing.work.due_at

    due_at = backend.now + sla_delay
    command = engine.context.commands.create(
        "escalate",
        target=incident,
        due_at=due_at,
        correlation_id=correlation_id,
        key=("itsm", incident.id, "sla-escalate"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def claim_next_incident(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    claim_id: str,
) -> str | None:
    if not engine.context.scenarios.attribute("itsm.support.available", True):
        return None

    request_id = f"support-agent:{claim_id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="support_agent",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return None

    get_id = f"incident-claim:{claim_id}"
    result = engine.stores.ensure_selection(
        backend,
        store_name="incident_queue",
        request_id=get_id,
        requested_at=backend.now,
    )

    if result is None:
        engine.resources.withdraw(backend, request_id)
        return None

    incident_id = str(result.item.value["incident_id"])
    incident = _incident(persistence, incident_id)
    if incident.state == "triaged":
        _dispatch(
            engine,
            incident,
            "assign",
            key=("itsm", incident.id, "assign"),
            correlation_id=flow_correlation_id(incident.id),
        )
        incident = _incident(persistence, incident.id)
    if incident.state == "assigned":
        _dispatch(
            engine,
            incident,
            "start",
            key=("itsm", incident.id, "start"),
            correlation_id=flow_correlation_id(incident.id),
        )
    return incident_id


def release_incident_owner(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    claim_id: str,
) -> None:
    engine.resources.withdraw(backend, f"support-agent:{claim_id}")


def resolve_incident(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    incident_id: str,
    claim_id: str | None = None,
    close: bool = False,
) -> None:
    incident = _incident(persistence, incident_id)
    if incident.state not in {"in_progress", "escalated", "resolved", "closed"}:
        raise RuntimeError(f"incident is not resolvable: {incident.state}")

    if incident.state == "escalated":
        escalation = _escalation(persistence, incident.id)
        if escalation is None or escalation.state != "completed":
            raise RuntimeError(
                "escalated incident requires completed durable escalation evidence"
            )

    if incident.state in {"in_progress", "escalated"}:
        _dispatch(
            engine,
            incident,
            "resolve",
            key=("itsm", incident.id, "resolve", incident.version),
            correlation_id=flow_correlation_id(incident.id),
        )
        cancel_sla(engine, incident.id)

    incident = _incident(persistence, incident.id)
    if close and incident.state == "resolved":
        _dispatch(
            engine,
            incident,
            "close",
            key=("itsm", incident.id, "close", incident.version),
            correlation_id=flow_correlation_id(incident.id),
        )

    if claim_id is not None:
        release_incident_owner(
            persistence,
            engine,
            backend,
            claim_id=claim_id,
        )


def reopen_incident(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    incident_id: str,
) -> None:
    incident = _incident(persistence, incident_id)
    if incident.state != "resolved":
        raise RuntimeError(f"incident is not reopenable: {incident.state}")
    _dispatch(
        engine,
        incident,
        "reopen",
        key=("itsm", incident.id, "reopen", incident.version),
        correlation_id=flow_correlation_id(incident.id),
    )


def ensure_escalation(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    incident_id: str,
) -> Escalation:
    existing = _escalation(persistence, incident_id)
    if existing is not None:
        return existing

    incident = _incident(persistence, incident_id)
    if incident.state != "escalated":
        raise RuntimeError("escalation requires Incident(escalated)")

    escalation = engine.context.entities.create(
        Escalation,
        key=("itsm-reference", incident.id, "escalation-1"),
        state="raised",
        attributes={"incident_id": incident.id},
    )
    with persistence.transaction() as uow:
        uow.save_entity(escalation)
    return escalation


def reconcile_escalation(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    incident_id: str,
    claim_id: str | None = None,
) -> bool:
    incident = _incident(persistence, incident_id)
    if incident.state != "escalated":
        return False

    escalation = ensure_escalation(
        persistence,
        engine,
        incident_id=incident.id,
    )
    request_id = f"escalation-manager:{escalation.id}"
    manager = engine.resources.ensure_requested(
        backend,
        resource_name="escalation_manager",
        request_id=request_id,
        requested_at=backend.now,
        priority=1,
    )
    if manager is None:
        return False

    correlation_id = flow_correlation_id(incident.id)
    escalation = _escalation(persistence, incident.id)
    if escalation is None:
        raise RuntimeError("escalation disappeared")

    for state, event in (
        ("raised", "acknowledge"),
        ("acknowledged", "take_ownership"),
        ("owned", "mitigate"),
        ("mitigated", "complete"),
    ):
        escalation = _escalation(persistence, incident.id)
        if escalation is not None and escalation.state == state:
            _dispatch(
                engine,
                escalation,
                event,
                key=("itsm-escalation", escalation.id, event),
                correlation_id=correlation_id,
            )

    resolve_incident(
        persistence,
        engine,
        backend,
        incident_id=incident.id,
    )
    engine.resources.withdraw(backend, request_id)
    if claim_id is not None:
        release_incident_owner(
            persistence,
            engine,
            backend,
            claim_id=claim_id,
        )
    return True


def run_happy_path() -> tuple[MemoryPersistence, ITSMEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
    )
    claimed = claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="happy",
    )
    if claimed != entities.incident_id:
        raise RuntimeError("reference incident was not claimed")
    resolve_incident(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
        claim_id="happy",
        close=True,
    )
    return persistence, entities


def run_escalation_path() -> tuple[MemoryPersistence, ITSMEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    sla_at = triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
    )
    claimed = claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="escalation",
    )
    if claimed != entities.incident_id:
        raise RuntimeError("reference incident was not claimed")
    backend.run_until(sla_at)
    if _incident(persistence, entities.incident_id).state != "escalated":
        raise RuntimeError("SLA escalation did not fire")
    if not reconcile_escalation(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
        claim_id="escalation",
    ):
        raise RuntimeError("escalation capacity unavailable")
    return persistence, entities
