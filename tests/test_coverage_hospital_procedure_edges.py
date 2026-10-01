from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.hospitals.patient_flow import (
    claim_next_ward_admission,
    triage_and_queue,
)
from sose.examples.hospitals.procedures import (
    commit_emergency_preemption,
    complete_emergency_and_resume,
    complete_procedure,
    ensure_emergency_episode,
    queue_treatment_episode,
    reconcile_preemption_business,
    reconcile_procedure_start,
    reconcile_scenario_emergency,
)
from sose.examples.hospitals.runtime import ORIGIN, build_runtime, seed_reference
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def _admitted_for_treatment():
    persistence, entities, engine, backend = _runtime()
    triage_and_queue(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    )
    claimed = claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="coverage-procedure",
    )
    assert claimed == entities.admission_id
    admission = persistence.entity("hospital_admission", entities.admission_id)
    assert admission is not None and admission.state == "treatment"
    return persistence, entities, engine, backend


def _started_procedure():
    persistence, entities, engine, backend = _admitted_for_treatment()
    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    )
    current = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert current is not None and current.state == "in_progress"
    return persistence, entities, engine, backend


def test_queue_procedure_requires_treatment_or_icu_admission():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="Admission\(treatment\|icu\)"):
        queue_treatment_episode(
            persistence,
            engine,
            episode_id=entities.treatment_episode_id,
        )


def test_queue_procedure_is_idempotent_after_planned_state():
    persistence, entities, engine, _ = _admitted_for_treatment()

    queue_treatment_episode(
        persistence,
        engine,
        episode_id=entities.treatment_episode_id,
    )
    first = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    events_before = tuple(persistence.events())

    queue_treatment_episode(
        persistence,
        engine,
        episode_id=entities.treatment_episode_id,
    )
    second = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )

    assert first is not None and first.state == "waiting_capacity"
    assert second == first
    assert tuple(persistence.events()) == events_before


def test_reconcile_procedure_start_is_idempotent_when_already_running():
    persistence, entities, engine, backend = _started_procedure()

    events_before = tuple(persistence.events())
    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    )
    assert tuple(persistence.events()) == events_before


def test_reconcile_procedure_start_rejects_terminal_episode():
    persistence, entities, engine, backend = _admitted_for_treatment()
    current = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert current is not None
    current.state = "cancelled"
    _save(persistence, current)

    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    ) is False


def test_reconcile_procedure_start_waits_for_capacity():
    persistence, entities, engine, backend = _admitted_for_treatment()
    blocker = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="procedure_suite",
        request_id="procedure:blocker",
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    assert blocker is not None

    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    ) is False
    current = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert current is not None and current.state == "waiting_capacity"


def test_ensure_emergency_episode_is_idempotent():
    persistence, entities, engine, _ = _started_procedure()

    first = ensure_emergency_episode(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    )
    second = ensure_emergency_episode(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    )

    assert second == first


@pytest.mark.parametrize(
    ("state", "expected"),
    [("planned", False), ("interrupted", True)],
)
def test_commit_emergency_preemption_handles_non_running_normal_state(state, expected):
    persistence, entities, engine, backend = _admitted_for_treatment()
    normal = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert normal is not None
    normal.state = state
    _save(persistence, normal)

    assert (
        commit_emergency_preemption(
            persistence,
            engine,
            backend,
            normal_episode_id=entities.treatment_episode_id,
        )
        is expected
    )


def test_commit_emergency_preemption_requires_preemption_evidence_if_normal_lease_is_missing():
    persistence, entities, engine, backend = _started_procedure()
    engine.preemptive_resources.withdraw(
        backend,
        f"procedure:{entities.treatment_episode_id}",
    )

    assert commit_emergency_preemption(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    ) is False


@pytest.mark.parametrize(
    ("state", "expected"),
    [("interrupted", True), ("planned", False)],
)
def test_reconcile_preemption_business_short_circuits_non_running_states(state, expected):
    persistence, entities, engine, _ = _admitted_for_treatment()
    normal = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert normal is not None
    normal.state = state
    _save(persistence, normal)

    assert (
        reconcile_preemption_business(
            persistence,
            engine,
            normal_episode_id=entities.treatment_episode_id,
        )
        is expected
    )


def test_complete_emergency_and_resume_returns_false_without_interrupt_boundary():
    persistence, entities, engine, backend = _admitted_for_treatment()

    assert complete_emergency_and_resume(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    ) is False


def test_complete_procedure_requires_active_episode():
    persistence, entities, engine, backend = _admitted_for_treatment()

    with pytest.raises(RuntimeError, match="procedure is not active"):
        complete_procedure(
            persistence,
            engine,
            backend,
            episode_id=entities.treatment_episode_id,
        )


def test_inactive_scenario_without_emergency_is_noop():
    persistence, entities, engine, backend = _admitted_for_treatment()

    assert reconcile_scenario_emergency(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    ) is False


def test_inactive_scenario_does_not_resume_after_emergency_already_completed_and_normal_running():
    persistence, entities, engine, backend = _started_procedure()
    emergency = ensure_emergency_episode(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    )
    emergency.state = "completed"
    _save(persistence, emergency)

    assert reconcile_scenario_emergency(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    ) is False
