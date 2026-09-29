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


def test_diagnostics_reports_failed_sink_delivery():
    from sose.jobs.model import SimulationJobState
    from sose.sinks.model import AnalyticalBatch, SinkDelivery
    from sose.core.events import DomainEvent

    persistence = MemoryPersistence()
    event = DomainEvent(
        event_id="sink-event",
        name="changed",
        entity_type="demo",
        entity_id="1",
        occurred_at=ORIGIN,
        tick=1,
    )
    batch = AnalyticalBatch(
        batch_id="batch-failed",
        job_id="job-1",
        domain_name="demo",
        config_revision=1,
        logical_tick=1,
        logical_time=ORIGIN,
        from_event_offset=0,
        to_event_offset=1,
        events=(event,),
    )
    with persistence.transaction() as uow:
        uow.save_job_state(
            SimulationJobState(
                job_id="job-1",
                domain_name="demo",
                config_json='{"start_at":"2026-01-01T00:00:00Z"}',
                config_revision=1,
                status="ready",
                initialized=True,
                logical_time=ORIGIN,
                next_tick=1,
            )
        )
        uow.save_sink_delivery(
            SinkDelivery(
                delivery_id="delivery-failed",
                sink_name="warehouse",
                batch=batch,
                attempts=1,
                last_error="RuntimeError: unavailable",
            )
        )

    diagnostics = collect_runtime_diagnostics(persistence)

    assert not diagnostics.healthy
    assert diagnostics.counts.sink_deliveries_pending == 1
    assert diagnostics.counts.sink_deliveries_failed == 1
    assert [issue.code for issue in diagnostics.issues] == [
        "sink.delivery_failed",
    ]
