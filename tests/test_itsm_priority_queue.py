from sose.backends.simpy import SimPyBackend
from sose.examples.itsm.simulation import (
    ORIGIN,
    build_runtime,
    claim_next_incident,
    create_incident,
    release_incident_owner,
    seed_reference,
    triage_and_queue,
)
from sose.persistence.memory import MemoryPersistence


def test_priority_queue_claims_lower_severity_number_first():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, severity=50)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    urgent = create_incident(
        persistence,
        engine,
        key="urgent",
        severity=1,
    )

    triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
    )
    triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=urgent.id,
    )

    first = claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="first",
    )
    assert first == urgent.id
    assert persistence.entity("itsm_incident", urgent.id).state == "in_progress"

    release_incident_owner(
        persistence,
        engine,
        backend,
        claim_id="first",
    )
    second = claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="second",
    )
    assert second == entities.incident_id
    assert persistence.entity(
        "itsm_incident", entities.incident_id
    ).state == "in_progress"
