import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.order_to_cash.simulation import (
    ORIGIN,
    build_runtime,
    collect_receivable,
    collection_case_id,
    ensure_collection_case,
    flow_correlation_id,
    reconcile_collection,
    reconcile_credit,
    reconcile_fulfillment,
    receivable_id,
    schedule_due,
    schedule_overdue,
    seed_reference,
    ship_invoice_and_ensure_receivable,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def _prepare_invoiced(persistence):
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_credit(
        persistence,
        engine,
        entities=entities,
    )
    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    receivable = ship_invoice_and_ensure_receivable(
        persistence,
        engine,
        entities=entities,
    )
    return entities, engine, backend, receivable


def test_happy_path_collects_before_overdue_and_cancels_deadline():
    persistence = MemoryPersistence()
    entities, engine, backend, receivable = _prepare_invoiced(persistence)

    due_at = schedule_due(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert len(persistence.scheduled_work()) == 1
    backend.run_until(due_at)

    receivable = persistence.entity("receivable", receivable.id)
    assert receivable is not None
    assert receivable.state == "due"

    overdue_at = schedule_overdue(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert overdue_at > backend.now
    assert len(persistence.scheduled_work()) == 1

    assert collect_receivable(
        persistence,
        engine,
        entities=entities,
    )
    receivable = persistence.entity("receivable", receivable.id)
    assert receivable is not None
    assert receivable.state == "collected"
    assert persistence.scheduled_work() == ()


def test_partial_fulfillment_is_explicit_before_completion():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_credit(
        persistence,
        engine,
        entities=entities,
    )
    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
        partial=True,
    ) is False

    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None
    assert order.state == "partial_fulfillment"

    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None
    assert order.state == "fulfilled"


def test_overdue_creates_collection_case_and_followup_escalates():
    persistence = MemoryPersistence()
    entities, engine, backend, receivable = _prepare_invoiced(persistence)

    due_at = schedule_due(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(due_at)
    overdue_at = schedule_overdue(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(overdue_at)

    receivable = persistence.entity("receivable", receivable.id)
    assert receivable is not None
    assert receivable.state == "overdue"

    case = ensure_collection_case(
        persistence,
        engine,
        entities=entities,
    )
    assert case.attributes["receivable_id"] == receivable.id

    assert reconcile_collection(
        persistence,
        engine,
        backend,
        entities=entities,
        promise=True,
    )
    case = persistence.entity("collection_case", case.id)
    assert case is not None
    assert case.state == "promised"
    assert len(persistence.scheduled_work()) == 1

    followup_at = persistence.scheduled_work()[0].due_at
    backend.run_until(followup_at)
    case = persistence.entity("collection_case", case.id)
    assert case is not None
    assert case.state == "escalated"

    assert collect_receivable(
        persistence,
        engine,
        entities=entities,
    )
    receivable = persistence.entity("receivable", receivable.id)
    case = persistence.entity("collection_case", case.id)
    assert receivable is not None and receivable.state == "collected"
    assert case is not None and case.state == "resolved"
    assert persistence.scheduled_work() == ()


def test_invoice_to_receivable_creation_is_idempotent_after_crash_boundary():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_credit(persistence, engine, entities=entities)
    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None
    correlation_id = flow_correlation_id(order.id)

    for event in ("ship", "invoice"):
        command = engine.context.commands.create(
            event,
            target=order,
            correlation_id=correlation_id,
            key=("o2c-crash-boundary", order.id, event),
        )
        engine.dispatch(command)
        order = persistence.entity("sales_order", entities.order_id)
        assert order is not None

    assert order.state == "invoiced"
    assert persistence.entity(
        "receivable",
        receivable_id(entities.order_id),
    ) is None

    first = ship_invoice_and_ensure_receivable(
        persistence,
        engine,
        entities=entities,
    )
    second = ship_invoice_and_ensure_receivable(
        persistence,
        engine,
        entities=entities,
    )
    assert first.id == second.id
    assert len(
        [
            entity
            for entity in (
                persistence.entity(
                    "receivable",
                    receivable_id(entities.order_id),
                ),
            )
            if entity is not None
        ]
    ) == 1


def test_due_schedule_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend, c_receivable = _prepare_invoiced(
        continuous
    )
    c_due = schedule_due(
        continuous,
        c_engine,
        c_backend,
        entities=c_entities,
    )
    c_backend.run_until(c_due)

    restarted = MemoryPersistence()
    r_entities, r_engine, r_backend_before, r_receivable = _prepare_invoiced(
        restarted
    )
    r_due = schedule_due(
        restarted,
        r_engine,
        r_backend_before,
        entities=r_entities,
    )
    restart_at = r_backend_before.now

    _, rebuilt_engine = build_runtime(
        restarted,
        now=restart_at,
    )
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    assert len(restarted.scheduled_work()) == 1
    rebuilt_backend.run_until(r_due)

    c_value = continuous.entity("receivable", c_receivable.id)
    r_value = restarted.entity("receivable", r_receivable.id)
    assert c_value is not None and r_value is not None
    assert c_value.state == r_value.state == "due"
    assert restarted.scheduled_work() == continuous.scheduled_work() == ()


def test_illegal_cross_entity_transitions_are_rejected():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    with pytest.raises(RuntimeError, match="requires SalesOrder\(invoiced\)"):
        from sose.examples.order_to_cash.simulation import ensure_receivable
        ensure_receivable(
            persistence,
            engine,
            entities=entities,
        )

    assert reconcile_credit(persistence, engine, entities=entities)
    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    ship_invoice_and_ensure_receivable(
        persistence,
        engine,
        entities=entities,
    )

    with pytest.raises(RuntimeError, match="overdue scheduling requires due"):
        schedule_overdue(
            persistence,
            engine,
            backend,
            entities=entities,
        )

    with pytest.raises(RuntimeError, match="requires Receivable\(overdue\)"):
        ensure_collection_case(
            persistence,
            engine,
            entities=entities,
        )
