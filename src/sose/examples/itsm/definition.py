from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import ITSMConfig
from .simulation import (
    build_runtime,
    claim_next_incident,
    reconcile_escalation,
    resolve_incident,
    seed_reference,
    triage_and_queue,
)

def _build(persistence: Persistence, config: ITSMConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: ITSMConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        severity=config.severity,
        service=config.service,
        support_agent_capacity=config.support_agent_capacity,
        escalation_manager_capacity=config.escalation_manager_capacity,
        incident_queue_capacity=config.incident_queue_capacity,
    )

def _incident_or_error(persistence, entities):
    incident = persistence.entity("itsm_incident", entities.incident_id)
    if incident is None:
        raise RuntimeError("configured incident was not persisted")
    return incident


def _reload_incident(persistence, incident_id: str):
    return persistence.entity("itsm_incident", incident_id)


def _reconcile_opened_incident(persistence, engine, backend, config, *, incident):
    if incident.state != "opened":
        return incident
    triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=incident.id,
        sla_delay=config.sla_delay,
    )
    return _reload_incident(persistence, incident.id)


def _reconcile_triaged_incident(persistence, engine, backend, *, incident, claim_id: str):
    if incident is None or incident.state != "triaged":
        return incident
    claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id=claim_id,
    )
    return _reload_incident(persistence, incident.id)


def _reconcile_escalated_incident(persistence, engine, backend, *, incident, claim_id: str):
    if incident is None or incident.state != "escalated":
        return incident
    reconcile_escalation(
        persistence,
        engine,
        backend,
        incident_id=incident.id,
        claim_id=claim_id,
    )
    return _reload_incident(persistence, incident.id)


def _reconcile_resolution(persistence, engine, backend, *, incident, claim_id: str):
    if incident is None:
        return
    if incident.state == "in_progress":
        resolve_incident(
            persistence,
            engine,
            backend,
            incident_id=incident.id,
            claim_id=claim_id,
            close=True,
        )
    elif incident.state == "resolved":
        resolve_incident(
            persistence,
            engine,
            backend,
            incident_id=incident.id,
            close=True,
        )


def _reconcile_tick(persistence, engine, backend, config, entities):
    incident = _incident_or_error(persistence, entities)
    claim_id = f"job:{entities.incident_id}"

    incident = _reconcile_opened_incident(
        persistence,
        engine,
        backend,
        config,
        incident=incident,
    )
    incident = _reconcile_triaged_incident(
        persistence,
        engine,
        backend,
        incident=incident,
        claim_id=claim_id,
    )
    incident = _reconcile_escalated_incident(
        persistence,
        engine,
        backend,
        incident=incident,
        claim_id=claim_id,
    )
    _reconcile_resolution(
        persistence,
        engine,
        backend,
        incident=incident,
        claim_id=claim_id,
    )

definition = DomainDefinition(
    name="itsm",
    description="IT service management reference domain.",
    config_model=ITSMConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","sla_delay"]),
)
