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
    queue_claim,
    satisfy_documents,
    seed_reference as seed_insurance,
)
from sose.examples.order_to_cash.simulation import (
    build_runtime as build_o2c_runtime,
    ensure_receivable,
    reconcile_credit,
    reconcile_fulfillment,
    seed_reference as seed_o2c,
    ship_invoice_and_ensure_receivable,
)
from sose.examples.record_to_report.simulation import (
    build_runtime as build_r2r_runtime,
    ensure_adjustment,
    reconcile_item,
    seed_reference as seed_r2r,
    submit_and_post_journal,
)
from sose.persistence.memory import MemoryPersistence


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
    backend = SimPyBackend()
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
    backend = SimPyBackend()
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
