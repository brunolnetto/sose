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

def _reconcile_tick(persistence, engine, backend, config, entities):
    incident = persistence.entity("itsm_incident", entities.incident_id)
    if incident is None:
        raise RuntimeError("configured incident was not persisted")
    claim_id = f"job:{entities.incident_id}"

    if incident.state == "opened":
        triage_and_queue(
            persistence,
            engine,
            backend,
            incident_id=incident.id,
            sla_delay=config.sla_delay,
        )
        incident = persistence.entity("itsm_incident", incident.id)

    if incident is not None and incident.state == "triaged":
        claim_next_incident(
            persistence,
            engine,
            backend,
            claim_id=claim_id,
        )
        incident = persistence.entity("itsm_incident", incident.id)

    if incident is not None and incident.state == "escalated":
        reconcile_escalation(
            persistence,
            engine,
            backend,
            incident_id=incident.id,
            claim_id=claim_id,
        )
        incident = persistence.entity("itsm_incident", incident.id)

    if incident is not None and incident.state == "in_progress":
        resolve_incident(
            persistence,
            engine,
            backend,
            incident_id=incident.id,
            claim_id=claim_id,
            close=True,
        )
    elif incident is not None and incident.state == "resolved":
        resolve_incident(
            persistence,
            engine,
            backend,
            incident_id=incident.id,
            close=True,
        )

definition = DomainDefinition(
    name="itsm",
    description="IT service management reference domain.",
    config_model=ITSMConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
)
