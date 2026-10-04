from sose.examples.itsm.simulation import (
    escalation_id,
    run_escalation_path,
    run_happy_path,
)


def test_itsm_happy_path_closes_without_stale_runtime_truth():
    persistence, entities = run_happy_path()

    assert persistence.entity("itsm_incident", entities.incident_id).state == "closed"
    assert persistence.store_items() == ()
    assert persistence.store_put_intents() == ()
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.resource_release_intents() == ()
    assert persistence.scheduled_work() == ()


def test_sla_escalation_is_independent_durable_case():
    persistence, entities = run_escalation_path()

    assert persistence.entity("itsm_incident", entities.incident_id).state == "resolved"
    escalation = persistence.entity(
        "itsm_escalation", escalation_id(entities.incident_id)
    )
    assert escalation is not None
    assert escalation.state == "completed"
    assert persistence.store_items() == ()
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.scheduled_work() == ()


def test_escalated_incident_cannot_resolve_without_completed_escalation_evidence():
    from sose.backends.simpy import SimPyBackend
    from sose.examples.itsm.simulation import (
        ORIGIN,
        build_runtime,
        claim_next_incident,
        resolve_incident,
        seed_reference,
        triage_and_queue,
    )
    from sose.persistence.memory import MemoryPersistence

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
    assert claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="gate",
    ) == entities.incident_id
    backend.run_until(sla_at)

    import pytest
    with pytest.raises(
        RuntimeError,
        match="completed durable escalation evidence",
    ):
        resolve_incident(
            persistence,
            engine,
            backend,
            incident_id=entities.incident_id,
            claim_id="gate",
        )
