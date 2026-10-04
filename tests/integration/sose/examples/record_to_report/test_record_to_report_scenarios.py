from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.record_to_report.scenarios import (
    close_team_shortage_scenario,
    posting_outage_scenario,
)
from sose.examples.record_to_report.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_close,
    reconcile_item,
    schedule_close,
    seed_reference,
    submit_and_post_journal,
)
from sose.persistence.memory import MemoryPersistence


def test_posting_outage_defers_posting_without_capacity_leak():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(posting_outage_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute("r2r.posting.available", True) is False

    assert submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()

    for _ in range(3):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    journal = persistence.entity("journal_entry", entities.journal_id)
    assert journal is not None and journal.state == "posted"


def test_close_team_shortage_preserves_in_progress_close_task_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(close_team_shortage_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert reconcile_item(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    # Scheduled scenarios are activated by advancing the engine clock.
    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute(
        "r2r.close_team.available",
        True,
    ) is False

    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
        delay=timedelta(hours=1),
    )
    backend.run_until(due_at)

    assert reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    task = persistence.entity("close_task", entities.close_task_id)
    period = persistence.entity("accounting_period", entities.period_id)
    assert task is not None and task.state == "in_progress"
    assert period is not None and period.state == "open"
    assert persistence.resource_demands() == ()

    for _ in range(4):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    period = persistence.entity("accounting_period", entities.period_id)
    assert period is not None and period.state == "closed"
