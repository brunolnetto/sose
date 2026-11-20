from __future__ import annotations

from datetime import datetime

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.persistence.memory import MemoryPersistence

from .entities import Escalation, Incident
from .scenarios import ORIGIN
from .foundation import (
    ITSMEntities,
    _dispatch,
    _escalation,
    _incident,
    build_runtime,
    cancel_sla,
    flow_correlation_id,
    release_incident_owner,
)
from .queueing import claim_next_incident, triage_and_queue


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    severity: int = 50,
    service: str = "payments-api",
    support_agent_capacity: int = 1,
    escalation_manager_capacity: int = 1,
    incident_queue_capacity: int = 100,
) -> ITSMEntities:
    context, engine = build_runtime(persistence, owner_id=owner_id, now=now)
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
    try:
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
        if claim_id is not None:
            release_incident_owner(
                persistence,
                engine,
                backend,
                claim_id=claim_id,
            )
        return True
    finally:
        engine.resources.withdraw(backend, request_id)


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, ITSMEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
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


def run_escalation_path(*, owner_id: str) -> tuple[MemoryPersistence, ITSMEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
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
