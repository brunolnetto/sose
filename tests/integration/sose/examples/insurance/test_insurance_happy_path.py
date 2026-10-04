from sose.backends.simpy import SimPyBackend
from sose.examples.insurance.simulation import (
    ORIGIN,
    build_runtime,
    claim_next_for_assessment,
    complete_assessment,
    ensure_document_request,
    ensure_reserve,
    payment_id,
    queue_claim,
    reconcile_payment,
    reserve_id,
    satisfy_documents,
    schedule_payment,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _prepare_approved_claim():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    ensure_document_request(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    satisfy_documents(persistence, engine, entities=entities)
    queue_claim(persistence, engine, backend, entities=entities)
    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="happy",
    ) == entities.claim_id
    assert complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="happy",
        outcome="approve",
    ) == "approved"
    reserve = ensure_reserve(persistence, engine, entities=entities)
    assert reserve.state == "established"
    return persistence, entities, engine, backend


def test_happy_path_reserves_schedules_and_pays_claim():
    persistence, entities, engine, backend = _prepare_approved_claim()

    due_at = schedule_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    payment = persistence.entity(
        "insurance_payment",
        payment_id(entities.claim_id),
    )
    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert payment is not None and payment.state == "scheduled"
    assert claim is not None and claim.state == "payment_scheduled"
    assert len(persistence.scheduled_work()) == 1

    backend.run_until(due_at)
    payment = persistence.entity("insurance_payment", payment.id)
    assert payment is not None and payment.state == "due"

    assert reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    claim = persistence.entity("insurance_claim", entities.claim_id)
    payment = persistence.entity("insurance_payment", payment.id)
    reserve = persistence.entity(
        "insurance_reserve",
        reserve_id(entities.claim_id),
    )
    assert claim is not None and claim.state == "paid"
    assert payment is not None and payment.state == "paid"
    assert payment.attributes["paid_amount"] == payment.attributes["amount"]
    assert reserve is not None and reserve.state == "released"
    assert persistence.scheduled_work() == ()
    assert persistence.resource_reservations() == ()


def test_partial_payout_does_not_mark_claim_paid():
    persistence, entities, engine, backend = _prepare_approved_claim()
    due_at = schedule_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(due_at)

    assert reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
        partial=True,
    ) is False

    claim = persistence.entity("insurance_claim", entities.claim_id)
    payment = persistence.entity(
        "insurance_payment",
        payment_id(entities.claim_id),
    )
    reserve = persistence.entity(
        "insurance_reserve",
        reserve_id(entities.claim_id),
    )
    assert claim is not None and claim.state == "payment_scheduled"
    assert payment is not None and payment.state == "partially_paid"
    assert payment.attributes["paid_amount"] == payment.attributes["amount"] / 2
    assert reserve is not None and reserve.state == "established"

    assert reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    claim = persistence.entity("insurance_claim", entities.claim_id)
    assert claim is not None and claim.state == "paid"
