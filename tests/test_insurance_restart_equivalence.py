from sose.backends.simpy import SimPyBackend
from sose.examples.insurance.simulation import (
    ORIGIN,
    build_runtime,
    claim_next_for_assessment,
    complete_assessment,
    document_request_id,
    ensure_document_request,
    ensure_reserve,
    payment_id,
    queue_claim,
    reconcile_payment,
    satisfy_documents,
    schedule_payment,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_document_deadline_survives_restart():
    continuous = MemoryPersistence()
    c_entities = seed_reference(continuous)
    _, c_engine = build_runtime(continuous)
    c_backend = SimPyBackend(origin=ORIGIN)
    c_engine.rebuild_backend(c_backend)
    c_request = ensure_document_request(
        continuous,
        c_engine,
        c_backend,
        entities=c_entities,
    )
    c_due = continuous.scheduled_work()[0].due_at
    c_backend.run_until(c_due)

    restarted = MemoryPersistence()
    r_entities = seed_reference(restarted)
    _, r_engine = build_runtime(restarted)
    r_backend_before = SimPyBackend(origin=ORIGIN)
    r_engine.rebuild_backend(r_backend_before)
    ensure_document_request(
        restarted,
        r_engine,
        r_backend_before,
        entities=r_entities,
    )
    r_due = restarted.scheduled_work()[0].due_at
    restart_at = r_backend_before.now

    _, rebuilt_engine = build_runtime(restarted, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)
    rebuilt_backend.run_until(r_due)

    c_value = continuous.entity(
        "insurance_document_request",
        c_request.id,
    )
    r_value = restarted.entity(
        "insurance_document_request",
        document_request_id(r_entities.claim_id),
    )
    assert c_value is not None and r_value is not None
    assert c_value.state == r_value.state == "expired"
    assert continuous.scheduled_work() == restarted.scheduled_work() == ()


def test_pending_adjuster_demand_survives_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    ensure_document_request(persistence, engine, backend, entities=entities)
    satisfy_documents(persistence, engine, entities=entities)
    queue_claim(persistence, engine, backend, entities=entities)

    engine.resources.request(
        backend,
        resource_name="claims_adjuster",
        request_id="adjuster-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="restart-worker",
    ) is None
    assert any(
        d.request_id == "claims-adjuster:restart-worker"
        for d in persistence.resource_demands()
    )

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    snapshot = rebuilt_backend.resource_snapshot("claims_adjuster")
    assert snapshot.in_use == 1
    assert snapshot.queued == 1

    blocker = next(
        r for r in persistence.resource_reservations()
        if r.request_id == "adjuster-blocker"
    )
    rebuilt_engine.resources.release(
        rebuilt_backend,
        blocker.reservation_id,
    )
    rebuilt_backend.run_until(rebuilt_backend.now)

    assert claim_next_for_assessment(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
        worker_id="restart-worker",
    ) == entities.claim_id


def _prepare_scheduled_payment(persistence):
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    ensure_document_request(persistence, engine, backend, entities=entities)
    satisfy_documents(persistence, engine, entities=entities)
    queue_claim(persistence, engine, backend, entities=entities)
    claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="payment-restart",
    )
    complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="payment-restart",
        outcome="approve",
    )
    ensure_reserve(persistence, engine, entities=entities)
    due_at = schedule_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    return entities, engine, backend, due_at


def test_payment_date_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend, c_due = _prepare_scheduled_payment(
        continuous
    )
    c_backend.run_until(c_due)
    assert reconcile_payment(
        continuous,
        c_engine,
        c_backend,
        entities=c_entities,
    )

    restarted = MemoryPersistence()
    r_entities, _, r_backend_before, r_due = _prepare_scheduled_payment(
        restarted
    )
    restart_at = r_backend_before.now
    _, rebuilt_engine = build_runtime(restarted, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)
    assert len(restarted.scheduled_work()) == 1

    rebuilt_backend.run_until(r_due)
    assert reconcile_payment(
        restarted,
        rebuilt_engine,
        rebuilt_backend,
        entities=r_entities,
    )

    c_payment = continuous.entity(
        "insurance_payment",
        payment_id(c_entities.claim_id),
    )
    r_payment = restarted.entity(
        "insurance_payment",
        payment_id(r_entities.claim_id),
    )
    c_claim = continuous.entity("insurance_claim", c_entities.claim_id)
    r_claim = restarted.entity("insurance_claim", r_entities.claim_id)
    assert c_payment is not None and r_payment is not None
    assert c_payment.state == r_payment.state == "paid"
    assert c_claim is not None and r_claim is not None
    assert c_claim.state == r_claim.state == "paid"
