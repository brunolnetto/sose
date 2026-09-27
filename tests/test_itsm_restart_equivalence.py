from sose.backends.simpy import SimPyBackend
from sose.examples.itsm.simulation import (
    ORIGIN,
    build_runtime,
    claim_next_incident,
    escalation_id,
    reconcile_escalation,
    seed_reference,
    triage_and_queue,
)
from sose.persistence.memory import MemoryPersistence


def _progress_to_owned_sla_wait(persistence):
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
    assert claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="restart",
    ) == entities.incident_id
    assert persistence.entity(
        "itsm_incident", entities.incident_id
    ).state == "in_progress"
    return entities, engine, backend, sla_at


def _finish(persistence, entities, engine, backend, sla_at):
    backend.run_until(sla_at)
    assert persistence.entity(
        "itsm_incident", entities.incident_id
    ).state == "escalated"
    assert reconcile_escalation(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
        claim_id="restart",
    )


def _snapshot(persistence, entities):
    return {
        "incident": persistence.entity("itsm_incident", entities.incident_id),
        "escalation": persistence.entity(
            "itsm_escalation", escalation_id(entities.incident_id)
        ),
        "events": persistence.events(),
        "scheduled": persistence.scheduled_work(),
        "position": persistence.simulation_position(),
        "items": persistence.store_items(),
        "get_results": persistence.store_get_results(),
        "demands": persistence.resource_demands(),
        "reservations": persistence.resource_reservations(),
        "release_intents": persistence.resource_release_intents(),
    }


def test_pending_sla_escalation_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend, c_sla = _progress_to_owned_sla_wait(
        continuous
    )
    _finish(
        continuous,
        c_entities,
        c_engine,
        c_backend,
        c_sla,
    )

    restarted = MemoryPersistence()
    r_entities, _, r_backend_before, r_sla = _progress_to_owned_sla_wait(
        restarted
    )
    restart_at = r_backend_before.now
    _, rebuilt_engine = build_runtime(
        restarted,
        now=restart_at,
    )
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    _finish(
        restarted,
        r_entities,
        rebuilt_engine,
        rebuilt_backend,
        r_sla,
    )

    assert _snapshot(restarted, r_entities) == _snapshot(
        continuous, c_entities
    )
