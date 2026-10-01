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
    emergency_request_id,
    ensure_emergency_episode,
    preemption_result,
    queue_treatment_episode,
    reconcile_preemption_business,
    reconcile_procedure_start,
    reconcile_scenario_emergency,
)
from sose.examples.hospitals.runtime import (
    ORIGIN,
    build_runtime,
    emergency_episode_id,
    seed_reference,
)
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


def _treatment_ready():
    persistence, entities, engine, backend = _runtime()
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
        claim_id="procedure-edge",
    ) == entities.admission_id
    admission = persistence.entity("hospital_admission", entities.admission_id)
    assert admission is not None and admission.state == "treatment"
    return persistence, entities, engine, backend


def _normal_in_progress():
    persistence, entities, engine, backend = _treatment_ready()
    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    )
    normal = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert normal is not None and normal.state == "in_progress"
    return persistence, entities, engine, backend


def test_queue_procedure_requires_treatment_or_icu_parent():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="Admission\(treatment\|icu\)"):
        queue_treatment_episode(
            persistence,
            engine,
            episode_id=entities.treatment_episode_id,
        )


def test_queue_procedure_is_idempotent_after_planned_transition():
    persistence, entities, engine, _ = _treatment_ready()

    queue_treatment_episode(
        persistence,
        engine,
        episode_id=entities.treatment_episode_id,
    )
    first = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert first is not None and first.state == "waiting_capacity"
    events_before = tuple(persistence.events())

    queue_treatment_episode(
        persistence,
        engine,
        episode_id=entities.treatment_episode_id,
    )

    assert tuple(persistence.events()) == events_before


def test_procedure_start_rejects_parent_outside_treatment_or_icu():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    ) is False


def test_procedure_start_returns_true_when_already_in_progress():
    persistence, entities, engine, backend = _normal_in_progress()
    events_before = tuple(persistence.events())

    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    ) is True
    assert tuple(persistence.events()) == events_before


def test_procedure_start_returns_false_for_terminal_episode():
    persistence, entities, engine, backend = _treatment_ready()
    episode = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert episode is not None
    episode.state = "completed"
    _save(persistence, episode)

    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=episode.id,
    ) is False


def test_procedure_start_waits_for_capacity_with_durable_demand():
    persistence, entities, engine, backend = _treatment_ready()
    blocker = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="procedure_suite",
        request_id="procedure:blocker",
        requested_at=backend.now,
        priority=1,
        preempt=False,
    )
    assert blocker is not None

    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    ) is False

    request_id = f"procedure:{entities.treatment_episode_id}"
    assert engine.preemptive_resources.has_request(request_id)
    assert engine.preemptive_resources.reservation_for(request_id) is None
    assert any(
        demand.request_id == request_id
        for demand in persistence.preemptive_resource_demands()
    )
    assert engine.preemptive_resources.reservation_for("procedure:blocker") == blocker


def test_emergency_episode_creation_is_idempotent():
    persistence, entities, engine, _ = _treatment_ready()

    first = ensure_emergency_episode(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    )
    entities_before = tuple(persistence.entities())
    second = ensure_emergency_episode(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    )

    assert second == first
    assert tuple(persistence.entities()) == entities_before


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("planned", False),
        ("interrupted", True),
    ],
)
def test_commit_preemption_short_circuits_non_active_normal_state(state, expected):
    persistence, entities, engine, backend = _treatment_ready()
    normal = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert normal is not None
    normal.state = state
    _save(persistence, normal)

    assert commit_emergency_preemption(
        persistence,
        engine,
        backend,
        normal_episode_id=normal.id,
    ) is expected


def test_commit_preemption_without_normal_reservation_or_result_waits():
    persistence, entities, engine, backend = _treatment_ready()
    normal = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    assert normal is not None
    normal.state = "in_progress"
    _save(persistence, normal)

    assert preemption_result(persistence, normal.id) is None
    assert commit_emergency_preemption(
        persistence,
        engine,
        backend,
        normal_episode_id=normal.id,
    ) is False


def test_business_preemption_is_idempotent_once_interrupted():
    persistence, entities, engine, backend = _normal_in_progress()
    assert commit_emergency_preemption(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    )
    assert reconcile_preemption_business(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    )
    events_before = tuple(persistence.events())

    assert reconcile_preemption_business(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    )
    assert tuple(persistence.events()) == events_before


def test_business_preemption_rejects_non_active_normal_episode():
    persistence, entities, engine, _ = _treatment_ready()

    assert reconcile_preemption_business(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    ) is False


def test_business_preemption_waits_until_backend_preemption_committed():
    persistence, entities, engine, _ = _normal_in_progress()

    assert preemption_result(persistence, entities.treatment_episode_id) is None
    assert reconcile_preemption_business(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    ) is False


def test_complete_emergency_resume_returns_false_before_preemption():
    persistence, entities, engine, backend = _normal_in_progress()

    assert complete_emergency_and_resume(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    ) is False


def test_complete_procedure_requires_active_episode():
    persistence, entities, engine, backend = _treatment_ready()

    with pytest.raises(RuntimeError, match="procedure is not active"):
        complete_procedure(
            persistence,
            engine,
            backend,
            episode_id=entities.treatment_episode_id,
        )


def test_scenario_emergency_inactive_without_emergency_is_noop(monkeypatch):
    persistence, entities, engine, backend = _normal_in_progress()
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=False: False,
    )

    assert reconcile_scenario_emergency(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    ) is False
    assert persistence.entity(
        "hospital_treatment_episode",
        emergency_episode_id(entities.treatment_episode_id),
    ) is None


def test_scenario_emergency_active_commits_and_interrupts(monkeypatch):
    persistence, entities, engine, backend = _normal_in_progress()
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=False: True
        if name == "hospital.procedure.emergency"
        else default,
    )

    assert reconcile_scenario_emergency(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    ) is True

    normal = persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    )
    emergency = persistence.entity(
        "hospital_treatment_episode",
        emergency_episode_id(entities.treatment_episode_id),
    )
    assert normal is not None and normal.state == "interrupted"
    assert emergency is not None and emergency.state == "in_progress"
    assert preemption_result(persistence, normal.id) is not None
    assert engine.preemptive_resources.reservation_for(
        emergency_request_id(normal.id)
    ) is not None
