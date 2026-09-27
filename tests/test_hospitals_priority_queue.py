from sose.backends.simpy import SimPyBackend
from sose.examples.hospitals.patient_flow import (
    claim_next_ward_admission,
    discharge_from_ward,
    triage_and_queue,
)
from sose.examples.hospitals.runtime import (
    ORIGIN,
    build_runtime,
    create_admission,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_triage_priority_claims_lower_acuity_number_first():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, acuity=50)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    urgent = create_admission(
        persistence,
        engine,
        key="urgent-admission",
        acuity=1,
    )

    triage_and_queue(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    )
    triage_and_queue(
        persistence,
        engine,
        backend,
        admission_id=urgent.id,
    )

    first = claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="first",
    )
    assert first == urgent.id
    discharge_from_ward(
        persistence,
        engine,
        backend,
        admission_id=urgent.id,
        claim_id="first",
    )

    second = claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="second",
    )
    assert second == entities.admission_id
