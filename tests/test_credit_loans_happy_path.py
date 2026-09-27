from sose.backends.simpy import SimPyBackend
from sose.examples.credit_loans.simulation import (
    ORIGIN,
    build_runtime,
    disburse_and_schedule,
    installment_id,
    loan_id,
    post_payment,
    reconcile_underwriting,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_approved_loan_generates_recurring_installments_and_pays_off():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, principal=1200.0, installment_count=3)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_underwriting(
        persistence, engine, backend, entities=entities, approve=True
    )
    due_dates = disburse_and_schedule(
        persistence, engine, backend, entities=entities
    )
    assert len(due_dates) == 3
    assert len(set(due_dates)) == 3
    loan = persistence.entity("loan", loan_id(entities.application_id))
    assert loan is not None and loan.state == "servicing"

    first_id = installment_id(loan.id, 1)
    backend.run_until(due_dates[0])
    first = persistence.entity("loan_installment", first_id)
    assert first is not None and first.state == "due"

    post_payment(
        persistence,
        engine,
        installment_id_value=first_id,
        payment_ordinal=1,
        amount=200.0,
    )
    first = persistence.entity("loan_installment", first_id)
    assert first is not None and first.state == "partially_paid"
    assert first.attributes["paid_amount"] == 200.0

    post_payment(
        persistence,
        engine,
        installment_id_value=first_id,
        payment_ordinal=2,
        amount=200.0,
    )
    assert persistence.entity("loan_installment", first_id).state == "paid"

    for ordinal, due_at in ((2, due_dates[1]), (3, due_dates[2])):
        backend.run_until(due_at)
        iid = installment_id(loan.id, ordinal)
        post_payment(
            persistence,
            engine,
            installment_id_value=iid,
            payment_ordinal=1,
            amount=400.0,
        )
        assert persistence.entity("loan_installment", iid).state == "paid"

    loan = persistence.entity("loan", loan.id)
    assert loan is not None and loan.state == "paid_off"
    assert loan.attributes["outstanding_balance"] == 0.0
    assert len(loan.attributes["applied_payment_ids"]) == 4


def test_rejected_application_never_creates_loan():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_underwriting(
        persistence, engine, backend, entities=entities, approve=False
    ) is False
    assert persistence.entity("loan", loan_id(entities.application_id)) is None
