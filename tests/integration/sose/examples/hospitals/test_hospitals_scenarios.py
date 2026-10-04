from sose.backends.simpy import SimPyBackend
from sose.examples.hospitals.patient_flow import (
    claim_next_ward_admission,
    triage_and_queue,
)
from sose.examples.hospitals.procedures import (
    reconcile_procedure_start,
    reconcile_scenario_emergency,
)
from sose.examples.hospitals.runtime import ORIGIN, build_runtime, seed_reference
from sose.examples.hospitals.scenarios import (
    emergency_procedure_surge_scenario,
    occupancy_surge_scenario,
)
from sose.persistence.memory import MemoryPersistence


def test_finite_occupancy_surge_preserves_triage_queue_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(occupancy_surge_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    triage_and_queue(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    )

    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute("hospital.ward.available", True) is False

    assert claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="surge",
    ) is None
    assert [
        item.value["admission_id"] for item in persistence.store_items()
    ] == [entities.admission_id]

    for _ in range(8):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert context.scenarios.attribute("hospital.ward.available", True) is True
    assert claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="recovered",
    ) == entities.admission_id


def test_finite_emergency_procedure_surge_interrupts_then_resumes():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(emergency_procedure_surge_scenario(),),
    )
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
        claim_id="procedure",
    ) == entities.admission_id
    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    )

    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute(
        "hospital.procedure.emergency", False
    ) is True
    assert reconcile_scenario_emergency(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    )
    assert persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    ).state == "interrupted"

    for _ in range(2):
        engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute(
        "hospital.procedure.emergency", False
    ) is False
    assert reconcile_scenario_emergency(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    )
    assert persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    ).state == "in_progress"
