from sose.backends.simpy import SimPyBackend
from sose.examples.record_to_report.simulation import (
    ORIGIN,
    adjustment_id,
    build_runtime,
    ensure_adjustment,
    flow_correlation_id,
    post_adjustment,
    reconcile_close,
    reconcile_item,
    schedule_close,
    seed_reference,
    submit_and_post_journal,
)
from sose.persistence.memory import MemoryPersistence


def test_posted_journal_cleanup_recovers_after_state_commit():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    request_id = f"posting:{entities.journal_id}"
    engine.resources.request(
        backend,
        resource_name="posting_processor",
        request_id=request_id,
        requested_at=backend.now,
        priority=100,
    )
    backend.run_until(backend.now)

    journal = persistence.entity("journal_entry", entities.journal_id)
    assert journal is not None
    correlation_id = flow_correlation_id(entities.period_id)
    for event in ("submit", "post"):
        command = engine.context.commands.create(
            event,
            target=journal,
            correlation_id=correlation_id,
            key=("r2r-crash-journal", journal.id, event),
        )
        engine.dispatch(command)
        journal = persistence.entity("journal_entry", entities.journal_id)
        assert journal is not None

    assert journal.state == "posted"
    assert any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )

    assert submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert not any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )


def test_matched_reconciliation_cleanup_recovers_after_state_commit():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    request_id = f"reconciliation-analyst:{entities.reconciliation_id}"
    engine.resources.request(
        backend,
        resource_name="reconciliation_analyst",
        request_id=request_id,
        requested_at=backend.now,
        priority=100,
    )
    backend.run_until(backend.now)

    item = persistence.entity(
        "reconciliation_item",
        entities.reconciliation_id,
    )
    assert item is not None
    correlation_id = flow_correlation_id(entities.period_id)
    for event in ("start", "match"):
        command = engine.context.commands.create(
            event,
            target=item,
            correlation_id=correlation_id,
            key=("r2r-crash-reconciliation", item.id, event),
        )
        engine.dispatch(command)
        item = persistence.entity(
            "reconciliation_item",
            entities.reconciliation_id,
        )
        assert item is not None

    assert item.state == "matched"
    assert reconcile_item(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert not any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )


def test_posted_adjustment_applies_missing_reconciliation_and_cleans_capacity():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
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
        outcome="unmatched",
    ) is False

    adjustment = ensure_adjustment(
        persistence,
        engine,
        entities=entities,
    )
    request_id = f"posting-adjustment:{adjustment.id}"
    engine.resources.request(
        backend,
        resource_name="posting_processor",
        request_id=request_id,
        requested_at=backend.now,
        priority=100,
    )
    backend.run_until(backend.now)

    correlation_id = flow_correlation_id(entities.period_id)
    for event in ("submit", "approve", "post"):
        command = engine.context.commands.create(
            event,
            target=adjustment,
            correlation_id=correlation_id,
            key=("r2r-crash-adjustment", adjustment.id, event),
        )
        engine.dispatch(command)
        adjustment = persistence.entity(
            "accounting_adjustment",
            adjustment_id(entities.reconciliation_id),
        )
        assert adjustment is not None

    item = persistence.entity(
        "reconciliation_item",
        entities.reconciliation_id,
    )
    assert adjustment.state == "posted"
    assert item is not None and item.state == "adjustment_required"

    assert post_adjustment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    item = persistence.entity(
        "reconciliation_item",
        entities.reconciliation_id,
    )
    assert item is not None and item.state == "reconciled"
    assert not any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )


def test_completed_close_cleanup_recovers_after_state_commit():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
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
    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
    )
    backend.run_until(due_at)

    request_id = f"close-accountant:{entities.close_task_id}"
    engine.resources.request(
        backend,
        resource_name="close_accountant",
        request_id=request_id,
        requested_at=backend.now,
        priority=100,
    )
    backend.run_until(backend.now)

    period = persistence.entity("accounting_period", entities.period_id)
    task = persistence.entity("close_task", entities.close_task_id)
    assert period is not None and task is not None
    correlation_id = flow_correlation_id(entities.period_id)

    for entity, event, key in (
        (period, "prepare_close", ("r2r-crash-close", period.id, "prepare")),
        (None, "close", None),
        (None, "complete", None),
    ):
        if event == "close":
            period = persistence.entity("accounting_period", entities.period_id)
            assert period is not None
            entity = period
            key = ("r2r-crash-close", period.id, "close")
        elif event == "complete":
            task = persistence.entity("close_task", entities.close_task_id)
            assert task is not None
            entity = task
            key = ("r2r-crash-close", task.id, "complete")
        command = engine.context.commands.create(
            event,
            target=entity,
            correlation_id=correlation_id,
            key=key,
        )
        engine.dispatch(command)

    assert persistence.entity(
        "accounting_period",
        entities.period_id,
    ).state == "closed"
    assert persistence.entity(
        "close_task",
        entities.close_task_id,
    ).state == "completed"

    assert reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert not any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )
