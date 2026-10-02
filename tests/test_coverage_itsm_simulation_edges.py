from __future__ import annotations

from datetime import timedelta

import pytest

import sose.examples.itsm.simulation as simulation
from sose.backends.simpy import SimPyBackend
from sose.examples.itsm.scenarios import ORIGIN, staff_shortage_scenario
from sose.examples.itsm.simulation import (
    _incident,
    build_runtime,
    claim_next_incident,
    ensure_escalation,
    reconcile_escalation,
    reopen_incident,
    resolve_incident,
    seed_reference,
    triage_and_queue,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(*, scenarios=(), **seed_kwargs):
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, **seed_kwargs)
    context, engine = build_runtime(persistence, scenarios=scenarios)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, context, engine, backend


def _set_incident_state(persistence, incident_id: str, state: str) -> None:
    with persistence.transaction() as uow:
        incident = uow.get_entity("itsm_incident", incident_id)
        assert incident is not None
        incident.state = state
        uow.save_entity(incident)


def _block(engine, backend, resource_name: str, request_id: str) -> None:
    engine.resources.request(
        backend,
        resource_name=resource_name,
        request_id=request_id,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)


def test_incident_lookup_and_triage_reject_missing_or_invalid_state():
    persistence, entities, _, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="was not persisted"):
        _incident(persistence, "missing")

    _set_incident_state(persistence, entities.incident_id, "closed")
    with pytest.raises(RuntimeError, match="not triaged"):
        triage_and_queue(
            persistence,
            engine,
            backend,
            incident_id=entities.incident_id,
        )


def test_triage_is_idempotent_for_queue_and_pending_sla():
    persistence, entities, _, engine, backend = _runtime()

    first = triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
        sla_delay=timedelta(hours=2),
    )
    second = triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
        sla_delay=timedelta(days=1),
    )

    assert second == first
    assert sum(
        item.item_id == f"incident-queue:{entities.incident_id}"
        for item in persistence.store_items()
    ) == 1


def test_claim_short_circuits_when_support_scenario_is_unavailable():
    persistence, entities, context, engine, backend = _runtime(
        scenarios=(staff_shortage_scenario(),)
    )
    triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
    )
    context.scenarios.on_tick()

    assert claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="shortage",
    ) is None


def test_claim_returns_none_when_support_capacity_is_busy():
    persistence, entities, _, engine, backend = _runtime()
    triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
    )
    _block(engine, backend, "support_agent", "support-blocker")

    assert claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="busy",
    ) is None


def test_claim_with_empty_queue_releases_support_agent():
    persistence, _, _, engine, backend = _runtime()

    assert claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="empty",
    ) is None
    assert not any(
        reservation.request_id == "support-agent:empty"
        for reservation in persistence.resource_reservations()
    )


def test_resolve_reopen_and_escalation_preconditions_are_enforced():
    persistence, entities, _, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="not resolvable"):
        resolve_incident(
            persistence,
            engine,
            backend,
            incident_id=entities.incident_id,
        )
    with pytest.raises(RuntimeError, match="not reopenable"):
        reopen_incident(
            persistence,
            engine,
            incident_id=entities.incident_id,
        )
    with pytest.raises(RuntimeError, match="requires Incident"):
        ensure_escalation(
            persistence,
            engine,
            incident_id=entities.incident_id,
        )

    _set_incident_state(persistence, entities.incident_id, "escalated")
    with pytest.raises(RuntimeError, match="completed durable escalation evidence"):
        resolve_incident(
            persistence,
            engine,
            backend,
            incident_id=entities.incident_id,
        )


def test_resolve_is_idempotent_for_resolved_and_closed_states():
    persistence, entities, _, engine, backend = _runtime()

    _set_incident_state(persistence, entities.incident_id, "resolved")
    resolve_incident(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
        close=False,
    )
    assert _incident(persistence, entities.incident_id).state == "resolved"

    resolve_incident(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
        close=True,
    )
    assert _incident(persistence, entities.incident_id).state == "closed"

    resolve_incident(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
    )
    assert _incident(persistence, entities.incident_id).state == "closed"


def test_reopen_resolved_incident_returns_it_to_progress():
    persistence, entities, _, engine, _ = _runtime()
    _set_incident_state(persistence, entities.incident_id, "resolved")

    reopen_incident(
        persistence,
        engine,
        incident_id=entities.incident_id,
    )

    assert _incident(persistence, entities.incident_id).state == "in_progress"


def test_ensure_escalation_is_durable_and_idempotent():
    persistence, entities, _, engine, _ = _runtime()
    _set_incident_state(persistence, entities.incident_id, "escalated")

    first = ensure_escalation(
        persistence,
        engine,
        incident_id=entities.incident_id,
    )
    second = ensure_escalation(
        persistence,
        engine,
        incident_id=entities.incident_id,
    )

    assert second == first


def test_reconcile_escalation_short_circuits_non_escalated_or_busy_manager():
    persistence, entities, _, engine, backend = _runtime()

    assert reconcile_escalation(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
    ) is False

    _set_incident_state(persistence, entities.incident_id, "escalated")
    _block(engine, backend, "escalation_manager", "manager-blocker")
    assert reconcile_escalation(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
    ) is False


def test_reconcile_escalation_detects_disappearing_evidence(monkeypatch):
    persistence, entities, _, engine, backend = _runtime()
    _set_incident_state(persistence, entities.incident_id, "escalated")

    real = simulation._escalation
    calls = 0

    def disappearing(persistence_arg, incident_id):
        nonlocal calls
        calls += 1
        value = real(persistence_arg, incident_id)
        # First lookup is ensure_escalation's pre-check; second lookup is the
        # post-capacity evidence check in reconcile_escalation.
        return None if calls >= 2 else value

    # Pre-create evidence so the first lookup returns a durable escalation.
    escalation = ensure_escalation(
        persistence,
        engine,
        incident_id=entities.incident_id,
    )
    assert escalation is not None
    monkeypatch.setattr(simulation, "_escalation", disappearing)

    with pytest.raises(RuntimeError, match="escalation disappeared"):
        reconcile_escalation(
            persistence,
            engine,
            backend,
            incident_id=entities.incident_id,
        )

    assert not any(
        reservation.request_id == f"escalation-manager:{escalation.id}"
        for reservation in persistence.resource_reservations()
    )


def test_reference_path_guards_are_executable(monkeypatch):
    monkeypatch.setattr(simulation, "claim_next_incident", lambda *args, **kwargs: "wrong")
    with pytest.raises(RuntimeError, match="reference incident was not claimed"):
        simulation.run_happy_path()
