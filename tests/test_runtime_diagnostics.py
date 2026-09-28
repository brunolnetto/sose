from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.diagnostics import collect_runtime_diagnostics
from sose.core.runtime import (
    ResourceDemand,
    ScheduledWork,
    StoreGetRequest,
)
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.memory import MemoryPersistence


def test_engine_diagnostics_reports_clean_pending_runtime():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=ORIGIN + timedelta(hours=2),
    )

    diagnostics = engine.diagnostics()

    assert diagnostics.healthy
    assert diagnostics.issues == ()
    assert diagnostics.counts.events == 1
    assert diagnostics.counts.scheduled_work == 1
    assert diagnostics.counts.resource_demands == 0
    assert diagnostics.counts.store_get_requests == 0


def test_diagnostics_follow_runtime_consumption_without_mutation():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    complete_at = ORIGIN + timedelta(hours=1)

    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=complete_at,
    )
    before = engine.diagnostics()
    backend.run_until(complete_at)
    after = engine.diagnostics()

    assert before.counts.scheduled_work == 1
    assert after.counts.scheduled_work == 0
    assert after.counts.events == 2
    assert after.healthy


def test_corruption_is_reported_with_stable_issue_codes():
    persistence = MemoryPersistence()

    persistence._state.scheduled_work["orphan-work"] = ScheduledWork(
        "orphan-work",
        ORIGIN + timedelta(hours=1),
        priority=100,
        sequence=1,
        command_id="missing-command",
    )
    persistence._state.resource_demands["orphan-demand"] = ResourceDemand(
        request_id="orphan-demand",
        resource_name="missing-resource",
        priority=100,
        requested_at=ORIGIN,
        sequence=1,
    )
    persistence._state.store_get_requests["orphan-get"] = StoreGetRequest(
        request_id="orphan-get",
        store_name="missing-store",
        requested_at=ORIGIN,
        sequence=1,
    )

    before = persistence._state
    diagnostics = collect_runtime_diagnostics(persistence)

    assert not diagnostics.healthy
    assert [issue.code for issue in diagnostics.issues] == [
        "scheduled.command_missing",
        "resource.definition_missing",
        "store.definition_missing",
    ]
    assert persistence._state is before


def test_diagnostics_are_deterministic_for_same_durable_truth():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=ORIGIN + timedelta(hours=3),
    )

    assert engine.diagnostics() == engine.diagnostics()
