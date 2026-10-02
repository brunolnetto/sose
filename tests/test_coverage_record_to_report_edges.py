from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.record_to_report.simulation import (
    ORIGIN,
    _entity,
    build_runtime,
    ensure_adjustment,
    post_adjustment,
    reconcile_close,
    reconcile_item,
    schedule_close,
    seed_reference,
    submit_and_post_journal,
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


@pytest.mark.parametrize(
    ("amount", "currency", "message"),
    [
        (0, "USD", "amount must be positive"),
        (-1, "USD", "amount must be positive"),
        (1, "", "currency must be non-empty"),
    ],
)
def test_seed_reference_validates_amount_and_currency(amount, currency, message):
    with pytest.raises(ValueError, match=message):
        seed_reference(MemoryPersistence(), amount=amount, currency=currency)


def test_missing_r2r_entity_guard_is_observable():
    with pytest.raises(RuntimeError, match="was not persisted"):
        _entity(MemoryPersistence(), "journal_entry", "missing")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("posted", True),
        ("rejected", False),
    ],
)
def test_journal_terminal_states_are_idempotent(state, expected):
    persistence, entities, engine, backend = _runtime()
    journal = persistence.entity("journal_entry", entities.journal_id)
    assert journal is not None
    journal.state = state
    _save(persistence, journal)

    assert (
        submit_and_post_journal(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is expected
    )
    assert persistence.resource_demands() == ()


def test_journal_posting_respects_processor_outage(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "r2r.posting.available"
        else default,
    )

    assert (
        submit_and_post_journal(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )
    journal = persistence.entity("journal_entry", entities.journal_id)
    assert journal is not None and journal.state == "drafted"
    assert persistence.resource_demands() == ()


def test_journal_posting_waits_for_processor_capacity():
    persistence, entities, engine, backend = _runtime()
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="posting_processor",
        request_id="posting:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert (
        submit_and_post_journal(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )
    journal = persistence.entity("journal_entry", entities.journal_id)
    assert journal is not None and journal.state == "drafted"
    assert any(
        demand.request_id == f"posting:{entities.journal_id}"
        for demand in persistence.resource_demands()
    )


def test_journal_rejection_path_is_durable_and_returns_false():
    persistence, entities, engine, backend = _runtime()

    assert (
        submit_and_post_journal(
            persistence,
            engine,
            backend,
            entities=entities,
            reject=True,
        )
        is False
    )
    journal = persistence.entity("journal_entry", entities.journal_id)
    assert journal is not None and journal.state == "rejected"


def _posted_runtime():
    persistence, entities, engine, backend = _runtime()
    assert submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    return persistence, entities, engine, backend


def test_reconciliation_rejects_unknown_outcome():
    persistence, entities, engine, backend = _posted_runtime()

    with pytest.raises(ValueError, match="unsupported reconciliation outcome"):
        reconcile_item(
            persistence,
            engine,
            backend,
            entities=entities,
            outcome="unknown",
        )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("matched", True),
        ("reconciled", True),
        ("rejected", False),
    ],
)
def test_reconciliation_terminal_states_are_idempotent(state, expected):
    persistence, entities, engine, backend = _posted_runtime()
    item = persistence.entity("reconciliation_item", entities.reconciliation_id)
    assert item is not None
    item.state = state
    _save(persistence, item)

    assert (
        reconcile_item(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is expected
    )
    assert not any(
        demand.request_id == f"reconciliation-analyst:{entities.reconciliation_id}"
        for demand in persistence.resource_demands()
    )


def test_reconciliation_waits_for_analyst_capacity():
    persistence, entities, engine, backend = _posted_runtime()
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="reconciliation_analyst",
        request_id="reconciliation:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert (
        reconcile_item(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )
    item = persistence.entity("reconciliation_item", entities.reconciliation_id)
    assert item is not None and item.state == "pending"


def test_ensure_adjustment_is_idempotent_after_creation():
    persistence, entities, engine, backend = _posted_runtime()
    assert (
        reconcile_item(
            persistence,
            engine,
            backend,
            entities=entities,
            outcome="unmatched",
        )
        is False
    )

    first = ensure_adjustment(persistence, engine, entities=entities)
    count_before = len(persistence.entities())
    second = ensure_adjustment(persistence, engine, entities=entities)

    assert second.id == first.id
    assert len(persistence.entities()) == count_before


def _adjustment_runtime():
    persistence, entities, engine, backend = _posted_runtime()
    assert (
        reconcile_item(
            persistence,
            engine,
            backend,
            entities=entities,
            outcome="unmatched",
        )
        is False
    )
    adjustment = ensure_adjustment(persistence, engine, entities=entities)
    return persistence, entities, engine, backend, adjustment


def test_posted_adjustment_replay_applies_pending_reconciliation_once():
    persistence, entities, engine, backend, adjustment = _adjustment_runtime()
    adjustment.state = "posted"
    _save(persistence, adjustment)

    assert post_adjustment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    item = persistence.entity("reconciliation_item", entities.reconciliation_id)
    assert item is not None and item.state == "reconciled"

    events_before = tuple(persistence.events())
    assert post_adjustment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert tuple(persistence.events()) == events_before


def test_rejected_adjustment_replay_returns_false():
    persistence, entities, engine, backend, adjustment = _adjustment_runtime()
    adjustment.state = "rejected"
    _save(persistence, adjustment)

    assert (
        post_adjustment(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )


def test_adjustment_posting_respects_processor_outage(monkeypatch):
    persistence, entities, engine, backend, adjustment = _adjustment_runtime()
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "r2r.posting.available"
        else default,
    )

    assert (
        post_adjustment(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )
    persisted = persistence.entity("accounting_adjustment", adjustment.id)
    assert persisted is not None and persisted.state == "proposed"


def test_adjustment_posting_waits_for_processor_capacity():
    persistence, entities, engine, backend, adjustment = _adjustment_runtime()
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="posting_processor",
        request_id="adjustment:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert (
        post_adjustment(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )
    persisted = persistence.entity("accounting_adjustment", adjustment.id)
    assert persisted is not None and persisted.state == "proposed"


def test_adjustment_rejection_path_is_durable():
    persistence, entities, engine, backend, adjustment = _adjustment_runtime()

    assert (
        post_adjustment(
            persistence,
            engine,
            backend,
            entities=entities,
            reject=True,
        )
        is False
    )
    persisted = persistence.entity("accounting_adjustment", adjustment.id)
    assert persisted is not None and persisted.state == "rejected"


def test_close_schedule_is_idempotent_and_in_progress_returns_now():
    persistence, entities, engine, backend = _runtime()
    first = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
    )
    second = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
    )
    assert second == first
    assert len(persistence.scheduled_work()) == 1

    task = persistence.entity("close_task", entities.close_task_id)
    assert task is not None
    task.state = "in_progress"
    _save(persistence, task)
    assert schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
    ) == backend.now


def test_close_schedule_rejects_terminal_task():
    persistence, entities, engine, backend = _runtime()
    task = persistence.entity("close_task", entities.close_task_id)
    assert task is not None
    task.state = "completed"
    _save(persistence, task)

    with pytest.raises(RuntimeError, match="cannot be scheduled"):
        schedule_close(
            persistence,
            engine,
            backend,
            task_id=task.id,
            period_id=entities.period_id,
        )


def _ready_close_runtime():
    persistence, entities, engine, backend = _posted_runtime()
    assert reconcile_item(
        persistence,
        engine,
        backend,
        entities=entities,
        outcome="match",
    )
    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
        delay=timedelta(),
    )
    backend.run_until(due_at)
    task = persistence.entity("close_task", entities.close_task_id)
    assert task is not None and task.state == "in_progress"
    return persistence, entities, engine, backend


def test_close_respects_team_outage(monkeypatch):
    persistence, entities, engine, backend = _ready_close_runtime()
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "r2r.close_team.available"
        else default,
    )

    assert (
        reconcile_close(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )
    period = persistence.entity("accounting_period", entities.period_id)
    assert period is not None and period.state == "open"


def test_close_waits_for_accountant_capacity():
    persistence, entities, engine, backend = _ready_close_runtime()
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="close_accountant",
        request_id="close:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert (
        reconcile_close(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )
    period = persistence.entity("accounting_period", entities.period_id)
    assert period is not None and period.state == "open"


def test_closed_completed_close_is_idempotent():
    persistence, entities, engine, backend = _ready_close_runtime()
    assert reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    events_before = tuple(persistence.events())

    assert reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert tuple(persistence.events()) == events_before
