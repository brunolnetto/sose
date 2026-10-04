from sose.backends.simpy import SimPyBackend
from sose.examples.hospitals.patient_flow import (
    claim_next_ward_admission,
    discharge_from_ward,
    triage_and_queue,
)
from sose.examples.hospitals.procedures import (
    commit_emergency_preemption,
    complete_emergency_and_resume,
    complete_procedure,
    reconcile_procedure_start,
)
from sose.examples.hospitals.runtime import (
    ORIGIN,
    build_runtime,
    emergency_episode_id,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def _snapshot(persistence, entities):
    return {
        "admission": persistence.entity(
            "hospital_admission", entities.admission_id
        ),
        "normal_episode": persistence.entity(
            "hospital_treatment_episode",
            entities.treatment_episode_id,
        ),
        "emergency_episode": persistence.entity(
            "hospital_treatment_episode",
            emergency_episode_id(entities.treatment_episode_id),
        ),
        "events": persistence.events(),
        "scheduled": persistence.scheduled_work(),
        "resources": persistence.resource_reservations(),
        "resource_demands": persistence.resource_demands(),
        "release_intents": persistence.resource_release_intents(),
        "preemptive_reservations": (
            persistence.preemptive_resource_reservations()
        ),
        "preemptive_demands": persistence.preemptive_resource_demands(),
        "preemptive_release_intents": (
            persistence.preemptive_resource_release_intents()
        ),
        "preemption_results": persistence.resource_preemption_results(),
        "store_items": persistence.store_items(),
        "store_puts": persistence.store_put_intents(),
        "store_gets": persistence.store_get_requests(),
        "store_results": persistence.store_get_results(),
    }


def _prepare_triage_wait(persistence):
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    triage_and_queue(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    )
    assert persistence.entity(
        "hospital_admission", entities.admission_id
    ).state == "waiting_bed"
    return entities, engine, backend


def _finish_ward(persistence, entities, engine, backend):
    assert claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="restart-ward",
    ) == entities.admission_id
    discharge_from_ward(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
        claim_id="restart-ward",
    )


def test_triage_wait_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend = _prepare_triage_wait(continuous)
    _finish_ward(
        continuous,
        c_entities,
        c_engine,
        c_backend,
    )

    restarted = MemoryPersistence()
    r_entities, _, r_backend_before = _prepare_triage_wait(restarted)
    rebuilt = restart_reference_runtime(
        restarted,
        build_runtime,
        r_backend_before,
        backend_factory=SimPyBackend,
    )
    rebuilt_engine = rebuilt.engine
    rebuilt_backend = rebuilt.backend

    assert rebuilt_backend.store_snapshot("triage_queue").size == 1
    _finish_ward(
        restarted,
        r_entities,
        rebuilt_engine,
        rebuilt_backend,
    )

    assert _snapshot(restarted, r_entities) == _snapshot(
        continuous, c_entities
    )


def _prepare_committed_preemption(persistence):
    entities = seed_reference(persistence)
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
        claim_id="restart-procedure",
    ) == entities.admission_id
    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    )
    assert commit_emergency_preemption(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    )

    # Deliberate crash boundary: resource preemption is durable but the
    # elective business episode has not yet recorded its interrupt.
    assert len(persistence.resource_preemption_results()) == 1
    assert persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    ).state == "in_progress"
    assert persistence.entity(
        "hospital_treatment_episode",
        emergency_episode_id(entities.treatment_episode_id),
    ).state == "in_progress"
    return entities, engine, backend


def _finish_preempted(persistence, entities, engine, backend):
    assert complete_emergency_and_resume(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    )
    assert persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    ).state == "in_progress"

    complete_procedure(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    )
    discharge_from_ward(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
        claim_id="restart-procedure",
    )


def test_committed_preemption_missing_interrupt_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend = _prepare_committed_preemption(
        continuous
    )
    _finish_preempted(
        continuous,
        c_entities,
        c_engine,
        c_backend,
    )

    restarted = MemoryPersistence()
    r_entities, _, r_backend_before = _prepare_committed_preemption(
        restarted
    )
    rebuilt = restart_reference_runtime(
        restarted,
        build_runtime,
        r_backend_before,
        backend_factory=SimPyBackend,
    )
    rebuilt_engine = rebuilt.engine
    rebuilt_backend = rebuilt.backend

    assert rebuilt_backend.preemptive_resource_snapshot(
        "procedure_suite"
    ).in_use == 1
    assert restarted.entity(
        "hospital_treatment_episode",
        r_entities.treatment_episode_id,
    ).state == "in_progress"

    _finish_preempted(
        restarted,
        r_entities,
        rebuilt_engine,
        rebuilt_backend,
    )

    assert _snapshot(restarted, r_entities) == _snapshot(
        continuous, c_entities
    )
    assert restarted.entity(
        "hospital_admission", r_entities.admission_id
    ).state == "discharged"
    assert len(restarted.resource_preemption_results()) == 1
