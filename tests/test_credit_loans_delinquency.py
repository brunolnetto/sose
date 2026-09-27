from sose.examples.credit_loans.entities import Payment
from sose.backends.simpy import SimPyBackend
from sose.examples.credit_loans.simulation import (
    COLLECTION_FOLLOWUP,
    ORIGIN,
    apply_restructure,
    build_runtime,
    collection_case_id,
    cure_delinquency,
    default_loan,
    delinquency_case_id,
    disburse_and_schedule,
    ensure_delinquency,
    flow_correlation_id,
    installment_id,
    loan_id,
    payment_id,
    post_payment,
    reconcile_collection,
    reconcile_underwriting,
    schedule_overdue,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _prepare_overdue():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    assert reconcile_underwriting(
        persistence, engine, backend, entities=entities, approve=True
    )
    due_dates = disburse_and_schedule(
        persistence, engine, backend, entities=entities
    )
    loan = persistence.entity("loan", loan_id(entities.application_id))
    iid = installment_id(loan.id, 1)
    backend.run_until(due_dates[0])
    overdue_at = schedule_overdue(
        persistence, engine, backend, installment_id_value=iid
    )
    backend.run_until(overdue_at)
    assert persistence.entity("loan_installment", iid).state == "overdue"
    return persistence, entities, engine, backend, loan.id, iid


def test_overdue_installment_enters_collection_and_can_default():
    persistence, entities, engine, backend, lid, iid = _prepare_overdue()
    delinquency = ensure_delinquency(
        persistence, engine, installment_id_value=iid
    )
    assert persistence.entity("loan", lid).state == "delinquent"

    assert reconcile_collection(
        persistence,
        engine,
        backend,
        installment_id_value=iid,
        promise=True,
    )
    collection = persistence.entity(
        "loan_collection_case", collection_case_id(delinquency.id)
    )
    assert collection is not None and collection.state == "promised"
    followup = engine.scheduler.find_pending(
        entity_type="loan_collection_case",
        entity_id=collection.id,
        name="escalate",
    )
    assert followup is not None
    backend.run_until(followup.work.due_at)
    collection = persistence.entity("loan_collection_case", collection.id)
    assert collection is not None and collection.state == "escalated"

    assert default_loan(persistence, engine, installment_id_value=iid)
    assert persistence.entity("loan", lid).state == "defaulted"
    assert persistence.entity(
        "delinquency_case",
        delinquency_case_id(lid, iid),
    ).state == "defaulted"


def test_restructure_preserves_old_delinquency_and_creates_new_obligation():
    persistence, entities, engine, backend, lid, iid = _prepare_overdue()
    delinquency = ensure_delinquency(
        persistence, engine, installment_id_value=iid
    )
    assert reconcile_collection(
        persistence, engine, backend, installment_id_value=iid
    )

    replacement = apply_restructure(
        persistence,
        engine,
        backend,
        installment_id_value=iid,
    )

    assert persistence.entity("loan_installment", iid).state == "restructured"
    assert persistence.entity(
        "loan_installment", installment_id(lid, 2)
    ).state == "restructured"
    assert persistence.entity(
        "loan_installment", installment_id(lid, 3)
    ).state == "restructured"
    assert persistence.entity("loan", lid).state == "servicing"
    assert persistence.entity(
        "delinquency_case", delinquency.id
    ).state == "restructured"
    assert replacement.state == "scheduled"
    assert replacement.attributes["amount"] == 1200.0
    assert engine.scheduler.find_pending(
        entity_type="loan_installment",
        entity_id=replacement.id,
        name="make_due",
    ) is not None


def test_restructured_installment_rejects_new_payment():
    persistence, entities, engine, backend, lid, iid = _prepare_overdue()
    ensure_delinquency(persistence, engine, installment_id_value=iid)
    replacement = apply_restructure(
        persistence,
        engine,
        backend,
        installment_id_value=iid,
    )
    balance_before = persistence.entity("loan", lid).attributes["outstanding_balance"]

    import pytest
    with pytest.raises(RuntimeError, match="new payment requires payable installment"):
        post_payment(
            persistence,
            engine,
            installment_id_value=iid,
            payment_ordinal=99,
            amount=100.0,
        )

    assert persistence.entity("loan", lid).attributes["outstanding_balance"] == balance_before
    assert replacement.attributes["amount"] == balance_before


def test_posted_payment_before_restructure_remains_reconcilable_without_double_obligation():
    persistence, entities, engine, backend, lid, iid = _prepare_overdue()
    ensure_delinquency(persistence, engine, installment_id_value=iid)

    pid = payment_id(iid, 1)
    payment = engine.context.entities.create(
        Payment,
        key=("credit-loans-reference", iid, "payment", 1),
        state="initiated",
        attributes={
            "loan_id": lid,
            "installment_id": iid,
            "ordinal": 1,
            "amount": 100.0,
        },
    )
    assert payment.id == pid
    with persistence.transaction() as uow:
        uow.save_entity(payment)
    command = engine.context.commands.create(
        "post",
        target=payment,
        correlation_id=flow_correlation_id(entities.application_id),
        key=("credit-payment-before-restructure", payment.id, "post"),
    )
    engine.dispatch(command)
    assert persistence.entity("loan_payment", pid).state == "posted"

    replacement = apply_restructure(
        persistence,
        engine,
        backend,
        installment_id_value=iid,
    )
    assert replacement.attributes["amount"] == 1200.0

    post_payment(
        persistence,
        engine,
        installment_id_value=iid,
        payment_ordinal=1,
        amount=100.0,
    )

    assert persistence.entity("loan", lid).attributes["outstanding_balance"] == 1100.0
    replacement = persistence.entity("loan_installment", replacement.id)
    assert replacement is not None and replacement.attributes["amount"] == 1100.0


def test_curing_one_delinquency_keeps_loan_delinquent_while_another_is_active():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, principal=600.0, installment_count=3)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    assert reconcile_underwriting(
        persistence, engine, backend, entities=entities, approve=True
    )
    due_dates = disburse_and_schedule(
        persistence, engine, backend, entities=entities
    )
    loan = persistence.entity("loan", loan_id(entities.application_id))
    assert loan is not None

    installment_ids = [installment_id(loan.id, 1), installment_id(loan.id, 2)]
    for iid, due_at in zip(installment_ids, due_dates[:2]):
        backend.run_until(due_at)
        overdue_at = schedule_overdue(
            persistence, engine, backend, installment_id_value=iid
        )
        backend.run_until(overdue_at)
        ensure_delinquency(persistence, engine, installment_id_value=iid)

    assert persistence.entity("loan", loan.id).state == "delinquent"

    post_payment(
        persistence,
        engine,
        installment_id_value=installment_ids[0],
        payment_ordinal=1,
        amount=200.0,
    )
    assert cure_delinquency(
        persistence, engine, installment_id_value=installment_ids[0]
    )
    assert persistence.entity("loan", loan.id).state == "delinquent"

    post_payment(
        persistence,
        engine,
        installment_id_value=installment_ids[1],
        payment_ordinal=1,
        amount=200.0,
    )
    assert cure_delinquency(
        persistence, engine, installment_id_value=installment_ids[1]
    )
    assert persistence.entity("loan", loan.id).state == "servicing"
