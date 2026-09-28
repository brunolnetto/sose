import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.cards_payments.simulation import (
    seed_reference as seed_cards,
)
from sose.examples.credit_loans.simulation import (
    ORIGIN as CREDIT_ORIGIN,
    build_runtime as build_credit_runtime,
    disburse_and_schedule,
    installment_id,
    loan_id,
    payment_id as credit_payment_id,
    post_payment,
    reconcile_underwriting,
    seed_reference as seed_credit,
)
from sose.examples.insurance.simulation import (
    ORIGIN as INSURANCE_ORIGIN,
    build_runtime as build_insurance_runtime,
    claim_next_for_assessment,
    complete_assessment,
    ensure_document_request,
    ensure_payment,
    ensure_reserve,
    payment_id as insurance_payment_id,
    queue_claim,
    reconcile_payment as reconcile_insurance_payment,
    satisfy_documents,
    schedule_payment as schedule_insurance_payment,
    seed_reference as seed_insurance,
)
from sose.examples.order_to_cash.simulation import (
    ORIGIN as O2C_ORIGIN,
    build_runtime as build_o2c_runtime,
    ensure_receivable,
    reconcile_credit,
    reconcile_fulfillment,
    seed_reference as seed_o2c,
    ship_invoice_and_ensure_receivable,
)
from sose.examples.record_to_report.simulation import (
    ORIGIN as R2R_ORIGIN,
    build_runtime as build_r2r_runtime,
    ensure_adjustment,
    reconcile_item,
    seed_reference as seed_r2r,
    submit_and_post_journal,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def test_cards_payment_declares_currency_with_amount():
    persistence = MemoryPersistence()
    entities = seed_cards(persistence)

    payment = persistence.entity("card_payment", entities.payment_id)

    assert payment is not None
    assert payment.attributes["amount"] == 125.0
    assert payment.attributes["currency"] == "USD"


def test_o2c_preserves_currency_from_order_to_receivable():
    persistence = MemoryPersistence()
    entities = seed_o2c(persistence, amount=250.0)
    _, engine = build_o2c_runtime(persistence)
    backend = SimPyBackend(origin=O2C_ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_credit(persistence, engine, entities=entities)
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
    order = persistence.entity("sales_order", entities.order_id)

    assert order is not None
    assert receivable.attributes["amount"] == order.attributes["amount"]
    assert receivable.attributes["currency"] == order.attributes["currency"] == "USD"


def test_r2r_preserves_currency_across_adjustment_lineage():
    persistence = MemoryPersistence()
    entities = seed_r2r(persistence, amount=1000.0, currency="BRL")
    _, engine = build_r2r_runtime(persistence)
    backend = SimPyBackend(origin=R2R_ORIGIN)
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
    adjustment = ensure_adjustment(persistence, engine, entities=entities)

    journal = persistence.entity("journal_entry", entities.journal_id)
    reconciliation = persistence.entity(
        "reconciliation_item",
        entities.reconciliation_id,
    )
    assert journal is not None and reconciliation is not None
    assert journal.attributes["currency"] == "BRL"
    assert reconciliation.attributes["currency"] == "BRL"
    assert adjustment.attributes["currency"] == "BRL"


def test_insurance_preserves_currency_from_claim_to_reserve_and_payment():
    persistence = MemoryPersistence()
    entities = seed_insurance(
        persistence,
        amount=5000.0,
        currency="EUR",
    )
    _, engine = build_insurance_runtime(persistence)
    backend = SimPyBackend(origin=INSURANCE_ORIGIN)
    engine.rebuild_backend(backend)

    ensure_document_request(persistence, engine, backend, entities=entities)
    satisfy_documents(persistence, engine, entities=entities)
    queue_claim(persistence, engine, backend, entities=entities)
    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="money-audit",
    ) == entities.claim_id
    assert complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="money-audit",
        outcome="approve",
    ) == "approved"

    reserve = ensure_reserve(persistence, engine, entities=entities)
    payment = ensure_payment(persistence, engine, entities=entities)
    claim = persistence.entity("insurance_claim", entities.claim_id)

    assert claim is not None
    assert claim.attributes["currency"] == "EUR"
    assert reserve.attributes["currency"] == "EUR"
    assert payment.attributes["currency"] == "EUR"


def test_credit_preserves_currency_into_installment_and_payment():
    persistence = MemoryPersistence()
    entities = seed_credit(persistence, principal=1200.0, installment_count=3)
    _, engine = build_credit_runtime(persistence)
    backend = SimPyBackend(origin=CREDIT_ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_underwriting(
        persistence,
        engine,
        backend,
        entities=entities,
        approve=True,
    )
    due_dates = disburse_and_schedule(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    loan = persistence.entity("loan", loan_id(entities.application_id))
    assert loan is not None

    first_id = installment_id(loan.id, 1)
    installment = persistence.entity("loan_installment", first_id)
    assert installment is not None
    assert installment.attributes["currency"] == loan.attributes["currency"] == "USD"

    backend.run_until(due_dates[0])
    payment = post_payment(
        persistence,
        engine,
        installment_id_value=first_id,
        payment_ordinal=1,
        amount=100.0,
    )
    persisted_payment = persistence.entity(
        "loan_payment",
        credit_payment_id(first_id, 1),
    )

    assert persisted_payment is not None
    assert payment.id == persisted_payment.id
    assert persisted_payment.attributes["currency"] == "USD"


def test_p2p_amount_is_quantity_not_money():
    # The current P2P Reference deliberately stops before invoice/matching/payment.
    # Its container request `amount` fields are inventory quantities, so P2P must
    # not be counted as executable monetary evidence by the Money audit.
    from sose.examples.p2p.simulation import seed_happy_path

    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence)
    purchase_order = persistence.entity("purchase_order", entities.purchase_order_id)

    assert purchase_order is not None
    assert "currency" not in purchase_order.attributes



def test_credit_rejects_fractional_cent_principal():
    with pytest.raises(ValueError, match="fractional cents"):
        seed_credit(MemoryPersistence(), principal=100.001, installment_count=3)


def test_credit_allocation_preserves_odd_cent_principal_exactly():
    persistence = MemoryPersistence()
    entities = seed_credit(persistence, principal=100.01, installment_count=3)
    _, engine = build_credit_runtime(persistence)
    backend = SimPyBackend(origin=CREDIT_ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_underwriting(
        persistence,
        engine,
        backend,
        entities=entities,
        approve=True,
    )
    disburse_and_schedule(persistence, engine, backend, entities=entities)
    loan = persistence.entity("loan", loan_id(entities.application_id))
    assert loan is not None

    amounts = [
        persistence.entity(
            "loan_installment",
            installment_id(loan.id, ordinal),
        ).attributes["amount"]
        for ordinal in range(1, 4)
    ]
    assert amounts == [33.33, 33.33, 33.35]
    assert sum(round(value * 100) for value in amounts) == 10001


def test_credit_rejects_fractional_cent_payment():
    persistence = MemoryPersistence()
    entities = seed_credit(persistence, principal=100.00, installment_count=1)
    _, engine = build_credit_runtime(persistence)
    backend = SimPyBackend(origin=CREDIT_ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_underwriting(
        persistence,
        engine,
        backend,
        entities=entities,
        approve=True,
    )
    due_at = disburse_and_schedule(
        persistence,
        engine,
        backend,
        entities=entities,
    )[0]
    loan = persistence.entity("loan", loan_id(entities.application_id))
    assert loan is not None
    iid = installment_id(loan.id, 1)
    backend.run_until(due_at)

    with pytest.raises(ValueError, match="fractional cents"):
        post_payment(
            persistence,
            engine,
            installment_id_value=iid,
            payment_ordinal=1,
            amount=10.005,
        )


def _prepare_insurance_payment(*, amount: float):
    persistence = MemoryPersistence()
    entities = seed_insurance(persistence, amount=amount, currency="USD")
    _, engine = build_insurance_runtime(persistence)
    backend = SimPyBackend(origin=INSURANCE_ORIGIN)
    engine.rebuild_backend(backend)
    ensure_document_request(persistence, engine, backend, entities=entities)
    satisfy_documents(persistence, engine, entities=entities)
    queue_claim(persistence, engine, backend, entities=entities)
    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="money-rounding",
    ) == entities.claim_id
    assert complete_assessment(
        persistence,
        engine,
        backend,
        entities=entities,
        worker_id="money-rounding",
        outcome="approve",
    ) == "approved"
    ensure_reserve(persistence, engine, entities=entities)
    due_at = schedule_insurance_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(due_at)
    return persistence, entities, engine, backend


def test_insurance_rejects_fractional_minor_unit_claim():
    with pytest.raises(ValueError, match="fractional minor units"):
        seed_insurance(MemoryPersistence(), amount=100.001, currency="USD")


def test_insurance_partial_payout_assigns_odd_cent_remainder_to_completion():
    persistence, entities, engine, backend = _prepare_insurance_payment(
        amount=100.01
    )

    assert reconcile_insurance_payment(
        persistence,
        engine,
        backend,
        entities=entities,
        partial=True,
    ) is False
    payment = persistence.entity(
        "insurance_payment",
        insurance_payment_id(entities.claim_id),
    )
    assert payment is not None
    assert payment.attributes["amount"] == 100.01
    assert payment.attributes["paid_amount"] == 50.0

    assert reconcile_insurance_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    payment = persistence.entity("insurance_payment", payment.id)
    assert payment is not None
    assert payment.attributes["paid_amount"] == 100.01


def test_insurance_odd_cent_partial_payout_is_restart_safe():
    persistence, entities, engine, backend = _prepare_insurance_payment(
        amount=100.01
    )
    assert reconcile_insurance_payment(
        persistence,
        engine,
        backend,
        entities=entities,
        partial=True,
    ) is False

    rebuilt = restart_reference_runtime(
        persistence,
        build_insurance_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    assert reconcile_insurance_payment(
        persistence,
        rebuilt.engine,
        rebuilt.backend,
        entities=entities,
    )

    payment = persistence.entity(
        "insurance_payment",
        insurance_payment_id(entities.claim_id),
    )
    assert payment is not None
    assert payment.state == "paid"
    assert payment.attributes["paid_amount"] == payment.attributes["amount"] == 100.01
