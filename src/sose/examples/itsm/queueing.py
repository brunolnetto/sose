"""ITSM triage, queue claim and reopen workflow (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .foundation import (
    SLA_DELAY,
    flow_correlation_id,
)


def _dispatch(*args, **kwargs):
    """Forward to ``simulation._dispatch`` at call time so patches on it still apply."""
    from . import simulation

    return simulation._dispatch(*args, **kwargs)


def _incident(*args, **kwargs):
    """Forward to ``simulation._incident`` at call time so patches on it still apply."""
    from . import simulation

    return simulation._incident(*args, **kwargs)


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
