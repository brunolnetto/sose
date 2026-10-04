from sose.backends.simpy import SimPyBackend
from sose.examples.hospitals.patient_flow import (
    claim_next_ward_admission,
    deteriorate_to_icu_wait,
    transfer_before_queue,
    transfer_from_icu_wait,
    triage_and_queue,
)
from sose.examples.hospitals.runtime import ORIGIN, build_runtime, seed_reference
from sose.persistence.memory import MemoryPersistence


def test_early_transfer_happens_before_ward_queue_commitment():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    transfer_before_queue(
        persistence,
        engine,
        admission_id=entities.admission_id,
    )

    assert persistence.entity(
        "hospital_admission", entities.admission_id
    ).state == "transferred"
    assert persistence.store_items() == ()
    assert persistence.store_put_intents() == ()


def test_icu_transfer_cancels_pending_demand_and_releases_partial_capacity():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, acuity=1)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    triage_and_queue(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    )
    assert claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="ward",
    ) == entities.admission_id
    deteriorate_to_icu_wait(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
        ward_claim_id="ward",
    )

    engine.resources.request(
        backend,
        resource_name="icu_bed",
        request_id="icu-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    # Establish durable pending ICU demand without transitioning to ICU.
    from sose.examples.hospitals.patient_flow import reconcile_icu_allocation

    assert reconcile_icu_allocation(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    ) is False

    transfer_from_icu_wait(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    )

    assert persistence.entity(
        "hospital_admission", entities.admission_id
    ).state == "transferred"
    assert not any(
        demand.request_id in {
            f"icu-bed:{entities.admission_id}",
            f"icu-team:{entities.admission_id}",
        }
        for demand in persistence.resource_demands()
    )
    assert not any(
        reservation.request_id in {
            f"icu-bed:{entities.admission_id}",
            f"icu-team:{entities.admission_id}",
        }
        for reservation in persistence.resource_reservations()
    )
