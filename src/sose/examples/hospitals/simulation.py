from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.persistence.memory import MemoryPersistence

from .patient_flow import (
    claim_next_ward_admission,
    deteriorate_to_icu_wait,
    discharge_from_icu,
    discharge_from_ward,
    reconcile_icu_allocation,
    triage_and_queue,
)
from .procedures import (
    commit_emergency_preemption,
    complete_emergency_and_resume,
    complete_procedure,
    reconcile_preemption_business,
    reconcile_procedure_start,
)
from .runtime import HospitalEntities, ORIGIN, build_runtime, seed_reference


def run_happy_path() -> tuple[MemoryPersistence, HospitalEntities]:
    persistence = MemoryPersistence()
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
    claimed = claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="happy",
    )
    if claimed != entities.admission_id:
        raise RuntimeError("reference admission was not allocated")

    discharge_from_ward(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
        claim_id="happy",
    )
    return persistence, entities


def run_icu_path() -> tuple[MemoryPersistence, HospitalEntities]:
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
    claimed = claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="icu",
    )
    if claimed != entities.admission_id:
        raise RuntimeError("reference admission was not allocated")

    deteriorate_to_icu_wait(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
        ward_claim_id="icu",
    )
    if not reconcile_icu_allocation(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    ):
        raise RuntimeError("ICU capacity unavailable")

    discharge_from_icu(
        persistence,
        engine,
        backend,
        admission_id=entities.admission_id,
    )
    return persistence, entities


def run_preemption_path() -> tuple[MemoryPersistence, HospitalEntities]:
    persistence = MemoryPersistence()
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
    claimed = claim_next_ward_admission(
        persistence,
        engine,
        backend,
        claim_id="procedure",
    )
    if claimed != entities.admission_id:
        raise RuntimeError("reference admission was not allocated")

    if not reconcile_procedure_start(
        persistence,
        engine,
        backend,
        episode_id=entities.treatment_episode_id,
    ):
        raise RuntimeError("procedure capacity unavailable")

    if not commit_emergency_preemption(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    ):
        raise RuntimeError("emergency preemption was not committed")

    if not reconcile_preemption_business(
        persistence,
        engine,
        normal_episode_id=entities.treatment_episode_id,
    ):
        raise RuntimeError("preemption did not reach business state")

    if not complete_emergency_and_resume(
        persistence,
        engine,
        backend,
        normal_episode_id=entities.treatment_episode_id,
    ):
        raise RuntimeError("elective procedure did not resume")

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
        claim_id="procedure",
    )
    return persistence, entities
