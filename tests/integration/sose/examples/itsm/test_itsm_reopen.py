from sose.backends.simpy import SimPyBackend
from sose.examples.itsm.simulation import (
    ORIGIN,
    build_runtime,
    claim_next_incident,
    release_incident_owner,
    reopen_incident,
    resolve_incident,
    seed_reference,
    triage_and_queue,
)
from sose.persistence.memory import MemoryPersistence


def test_resolved_incident_can_reopen_without_erasing_resolution_history():
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
    assert claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="owner",
    ) == entities.incident_id

    resolve_incident(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
    )
    assert persistence.entity(
        "itsm_incident", entities.incident_id
    ).state == "resolved"

    before = len(persistence.events())
    reopen_incident(
        persistence,
        engine,
        incident_id=entities.incident_id,
    )
    assert persistence.entity(
        "itsm_incident", entities.incident_id
    ).state == "in_progress"
    assert len(persistence.events()) == before + 1

    resolve_incident(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
        claim_id="owner",
    )
    assert persistence.entity(
        "itsm_incident", entities.incident_id
    ).state == "resolved"
