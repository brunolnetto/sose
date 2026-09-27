from sose.backends.simpy import SimPyBackend
from sose.examples.order_to_cash.simulation import (
    ORIGIN,
    build_runtime,
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


def _prepare_fulfilled(persistence):
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
    return entities, engine, backend


def _prepare_overdue(persistence):
    entities, engine, backend = _prepare_fulfilled(persistence)
    receivable = ship_invoice_and_ensure_receivable(
        persistence,
        engine,
        entities=entities,
    )
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
    assert receivable is not None and receivable.state == "overdue"
    return entities, engine, backend, receivable


def test_invoiced_order_recovers_missing_receivable_after_restart():
    persistence = MemoryPersistence()
    entities, engine, backend = _prepare_fulfilled(persistence)
    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None

    correlation_id = flow_correlation_id(order.id)
    for event in ("ship", "invoice"):
        command = engine.context.commands.create(
            event,
            target=order,
            correlation_id=correlation_id,
            key=("o2c-invoice-restart", order.id, event),
        )
        engine.dispatch(command)
        order = persistence.entity("sales_order", entities.order_id)
        assert order is not None

    assert order.state == "invoiced"
    assert persistence.entity(
        "receivable",
        receivable_id(entities.order_id),
    ) is None

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    receivable = ship_invoice_and_ensure_receivable(
        persistence,
        rebuilt_engine,
        entities=entities,
    )
    assert receivable.id == receivable_id(entities.order_id)
    assert receivable.attributes["order_id"] == entities.order_id


def test_overdue_receivable_recovers_missing_collection_case_after_restart():
    persistence = MemoryPersistence()
    entities, _, backend, receivable = _prepare_overdue(persistence)

    assert persistence.entity(
        "collection_case",
        collection_case_id(receivable.id),
    ) is None

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    first = ensure_collection_case(
        persistence,
        rebuilt_engine,
        entities=entities,
    )
    second = ensure_collection_case(
        persistence,
        rebuilt_engine,
        entities=entities,
    )
    assert first.id == second.id == collection_case_id(receivable.id)


def _prepare_promised_followup(persistence):
    entities, engine, backend, receivable = _prepare_overdue(persistence)
    case = ensure_collection_case(
        persistence,
        engine,
        entities=entities,
    )
    assert reconcile_collection(
        persistence,
        engine,
        backend,
        entities=entities,
        promise=True,
    )
    case = persistence.entity("collection_case", case.id)
    assert case is not None and case.state == "promised"
    assert len(persistence.scheduled_work()) == 1
    due_at = persistence.scheduled_work()[0].due_at
    return entities, engine, backend, case, due_at


def test_collection_followup_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend, c_case, c_due = _prepare_promised_followup(
        continuous
    )
    c_backend.run_until(c_due)

    restarted = MemoryPersistence()
    r_entities, _, r_backend_before, r_case, r_due = _prepare_promised_followup(
        restarted
    )
    restart_at = r_backend_before.now
    _, rebuilt_engine = build_runtime(restarted, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    assert len(restarted.scheduled_work()) == 1
    rebuilt_backend.run_until(r_due)

    c_value = continuous.entity("collection_case", c_case.id)
    r_value = restarted.entity("collection_case", r_case.id)
    assert c_value is not None and r_value is not None
    assert c_value.state == r_value.state == "escalated"
    assert continuous.scheduled_work() == restarted.scheduled_work() == ()


def test_pending_fulfillment_capacity_survives_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_credit(persistence, engine, entities=entities)

    engine.resources.request(
        backend,
        resource_name="fulfillment_team",
        request_id="fulfillment-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert any(
        demand.request_id == f"fulfillment-team:{entities.order_id}"
        for demand in persistence.resource_demands()
    )

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    snapshot = rebuilt_backend.resource_snapshot("fulfillment_team")
    assert snapshot.in_use == 1
    assert snapshot.queued == 1

    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "fulfillment-blocker"
    )
    rebuilt_engine.resources.release(
        rebuilt_backend,
        blocker.reservation_id,
    )
    rebuilt_backend.run_until(rebuilt_backend.now)

    assert reconcile_fulfillment(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
        entities=entities,
    )
    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None and order.state == "fulfilled"


def test_pending_collection_capacity_survives_restart():
    persistence = MemoryPersistence()
    entities, engine, backend, receivable = _prepare_overdue(persistence)
    case = ensure_collection_case(
        persistence,
        engine,
        entities=entities,
    )

    engine.resources.request(
        backend,
        resource_name="collection_agent",
        request_id="collection-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert reconcile_collection(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert any(
        demand.request_id == f"collection-agent:{case.id}"
        for demand in persistence.resource_demands()
    )

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    snapshot = rebuilt_backend.resource_snapshot("collection_agent")
    assert snapshot.in_use == 1
    assert snapshot.queued == 1

    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "collection-blocker"
    )
    rebuilt_engine.resources.release(
        rebuilt_backend,
        blocker.reservation_id,
    )
    rebuilt_backend.run_until(rebuilt_backend.now)

    assert reconcile_collection(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
        entities=entities,
    )
    case = persistence.entity("collection_case", case.id)
    assert case is not None and case.state == "contacted"
