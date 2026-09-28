from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    run_happy_path,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def test_tutorial_happy_path_completes_from_durable_schedule():
    persistence, job_id = run_happy_path()

    job = persistence.entity("tutorial_job", job_id)
    assert job is not None and job.state == "completed"
    assert persistence.scheduled_work() == ()


def test_tutorial_job_survives_backend_restart():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    complete_at = ORIGIN + timedelta(hours=2)

    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=complete_at,
    )
    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(complete_at)

    persisted = persistence.entity("tutorial_job", job.id)
    assert persisted is not None and persisted.state == "completed"
    assert persistence.scheduled_work() == ()
