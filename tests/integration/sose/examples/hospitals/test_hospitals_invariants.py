import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.hospitals.procedures import (
    reconcile_preemption_business,
    reconcile_procedure_start,
)
from sose.examples.hospitals.runtime import ORIGIN, build_runtime, seed_reference
from sose.persistence.memory import MemoryPersistence


def test_procedure_cannot_start_before_admission_treatment():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    ) is False
    assert persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    ).state == "planned"


def test_business_interrupt_requires_durable_preemption_evidence():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    # Force only the business state for this invariant test.
    admission = persistence.entity("hospital_admission", entities.admission_id)
    episode = persistence.entity(
        "hospital_treatment_episode", entities.treatment_episode_id
    )
    assert admission is not None and episode is not None

    # There is no legal runtime path to in_progress without capacity; directly
    # persisting this synthetic boundary lets the reconciler invariant be tested.
    episode.state = "in_progress"
    with persistence.transaction() as uow:
        uow.save_entity(episode)

    assert reconcile_preemption_business(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    ) is False
    assert persistence.entity(
        "hospital_treatment_episode",
        entities.treatment_episode_id,
    ).state == "in_progress"
