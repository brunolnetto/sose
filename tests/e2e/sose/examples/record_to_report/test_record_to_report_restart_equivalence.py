from sose.backends.simpy import SimPyBackend
from sose.examples.record_to_report.simulation import (
    ORIGIN,
    adjustment_id,
    build_runtime,
    ensure_adjustment,
    reconcile_close,
    reconcile_item,
    schedule_close,
    seed_reference,
    submit_and_post_journal,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def _prepare_matched(persistence):
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
    return entities, engine, backend


def test_pending_close_schedule_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend = _prepare_matched(continuous)
    c_due = schedule_close(
        continuous,
        c_engine,
        c_backend,
        task_id=c_entities.close_task_id,
        period_id=c_entities.period_id,
    )
    c_backend.run_until(c_due)
    assert reconcile_close(
        continuous,
        c_engine,
        c_backend,
        entities=c_entities,
    )

    restarted = MemoryPersistence()
    r_entities, r_engine, r_backend_before = _prepare_matched(restarted)
    r_due = schedule_close(
        restarted,
        r_engine,
        r_backend_before,
        task_id=r_entities.close_task_id,
        period_id=r_entities.period_id,
    )
    rebuilt = restart_reference_runtime(
        restarted,
        build_runtime,
        r_backend_before,
        backend_factory=SimPyBackend,
    )
    rebuilt_engine = rebuilt.engine
    rebuilt_backend = rebuilt.backend

    assert len(restarted.scheduled_work()) == 1
    rebuilt_backend.run_until(r_due)
    assert reconcile_close(
        restarted,
        rebuilt_engine,
        rebuilt_backend,
        entities=r_entities,
    )

    c_period = continuous.entity("accounting_period", c_entities.period_id)
    r_period = restarted.entity("accounting_period", r_entities.period_id)
    c_task = continuous.entity("close_task", c_entities.close_task_id)
    r_task = restarted.entity("close_task", r_entities.close_task_id)
    assert c_period == r_period
    assert c_task == r_task
    assert continuous.scheduled_work() == restarted.scheduled_work() == ()


def test_pending_close_accountant_demand_survives_restart():
    persistence = MemoryPersistence()
    entities, engine, backend = _prepare_matched(persistence)

    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
    )
    backend.run_until(due_at)

    engine.resources.request(
        backend,
        resource_name="close_accountant",
        request_id="close-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert any(
        demand.request_id == f"close-accountant:{entities.close_task_id}"
        for demand in persistence.resource_demands()
    )

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt_engine = rebuilt.engine
    rebuilt_backend = rebuilt.backend

    snapshot = rebuilt_backend.resource_snapshot("close_accountant")
    assert snapshot.in_use == 1
    assert snapshot.queued == 1

    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "close-blocker"
    )
    rebuilt_engine.resources.release(
        rebuilt_backend,
        blocker.reservation_id,
    )
    rebuilt_backend.run_until(rebuilt_backend.now)

    assert reconcile_close(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
        entities=entities,
    )
    period = persistence.entity("accounting_period", entities.period_id)
    assert period is not None and period.state == "closed"


def test_unmatched_item_recovers_missing_adjustment_after_restart():
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
    item = persistence.entity(
        "reconciliation_item",
        entities.reconciliation_id,
    )
    assert item is not None and item.state == "unmatched"
    assert persistence.entity(
        "accounting_adjustment",
        adjustment_id(entities.reconciliation_id),
    ) is None

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt_engine = rebuilt.engine
    rebuilt_backend = rebuilt.backend

    first = ensure_adjustment(
        persistence,
        rebuilt_engine,
        entities=entities,
    )
    second = ensure_adjustment(
        persistence,
        rebuilt_engine,
        entities=entities,
    )
    assert first.id == second.id == adjustment_id(entities.reconciliation_id)
