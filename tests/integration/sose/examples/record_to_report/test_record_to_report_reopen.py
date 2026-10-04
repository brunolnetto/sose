from sose.backends.simpy import SimPyBackend
from sose.examples.record_to_report.simulation import (
    ORIGIN,
    REOPEN_CLOSE_DELAY,
    build_runtime,
    reconcile_close,
    reopen_period,
    run_happy_path,
    schedule_close,
)


def test_closed_period_reopens_with_distinct_close_task_and_recloses():
    persistence, entities = run_happy_path()
    restart_at = persistence.simulation_position().logical_time
    _, engine = build_runtime(persistence, now=restart_at)
    backend = SimPyBackend(origin=restart_at)
    engine.rebuild_backend(backend)
    backend.run_until(restart_at)

    task = reopen_period(
        persistence,
        engine,
        entities=entities,
    )
    period = persistence.entity("accounting_period", entities.period_id)
    assert period is not None and period.state == "reopened"
    assert task.id != entities.close_task_id
    assert task.attributes["ordinal"] == 2

    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=task.id,
        period_id=entities.period_id,
        delay=REOPEN_CLOSE_DELAY,
    )
    backend.run_until(due_at)

    assert reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
        task_id=task.id,
    )
    period = persistence.entity("accounting_period", entities.period_id)
    task = persistence.entity("close_task", task.id)
    assert period is not None and period.state == "closed"
    assert task is not None and task.state == "completed"
