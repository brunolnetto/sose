from sose.backends.simpy import SimPyBackend
from sose.examples.credit_loans.entities import Payment
from sose.examples.credit_loans.simulation import (
    ORIGIN,
    build_runtime,
    collection_case_id,
    credit_decision_id,
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
from sose.testing.restart import restart_reference_runtime


def _prepare_servicing():
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
    assert loan is not None
    return persistence, entities, engine, backend, loan, due_dates


def test_pending_installment_schedule_survives_restart():
    persistence, entities, _, backend, loan, due_dates = _prepare_servicing()

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    assert len(persistence.scheduled_work()) == 3

    rebuilt.backend.run_until(due_dates[0])
    first = persistence.entity("loan_installment", installment_id(loan.id, 1))
    assert first is not None and first.state == "due"
    assert len(persistence.scheduled_work()) == 2


def test_posted_payment_is_applied_once_after_restart():
    persistence, entities, engine, backend, loan, due_dates = _prepare_servicing()
    iid = installment_id(loan.id, 1)
    backend.run_until(due_dates[0])
    installment = persistence.entity("loan_installment", iid)
    assert installment is not None and installment.state == "due"

    pid = payment_id(iid, 1)
    payment = engine.context.entities.create(
        Payment,
        key=("credit-loans-reference", iid, "payment", 1),
        state="initiated",
        attributes={
            "loan_id": loan.id,
            "installment_id": iid,
            "ordinal": 1,
            "amount": 200.0,
        },
    )
    assert payment.id == pid
    with persistence.transaction() as uow:
        uow.save_entity(payment)
    command = engine.context.commands.create(
        "post",
        target=payment,
        correlation_id=flow_correlation_id(entities.application_id),
        key=("credit-payment-crash", payment.id, "post"),
    )
    engine.dispatch(command)

    assert persistence.entity("loan_payment", pid).state == "posted"
    assert persistence.entity("loan_installment", iid).attributes["paid_amount"] == 0.0
    assert persistence.entity("loan", loan.id).attributes["outstanding_balance"] == 1200.0

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    post_payment(
        persistence,
        rebuilt.engine,
        installment_id_value=iid,
        payment_ordinal=1,
        amount=200.0,
    )
    first_balance = persistence.entity("loan", loan.id).attributes["outstanding_balance"]
    assert first_balance == 1000.0
    assert persistence.entity("loan_installment", iid).attributes["paid_amount"] == 200.0

    post_payment(
        persistence,
        rebuilt.engine,
        installment_id_value=iid,
        payment_ordinal=1,
        amount=200.0,
    )
    assert persistence.entity("loan", loan.id).attributes["outstanding_balance"] == 1000.0
    assert persistence.entity("loan_installment", iid).attributes["paid_amount"] == 200.0


def test_overdue_installment_recovers_missing_delinquency_case_after_restart():
    persistence, _, engine, backend, loan, due_dates = _prepare_servicing()
    iid = installment_id(loan.id, 1)
    backend.run_until(due_dates[0])
    overdue_at = schedule_overdue(
        persistence, engine, backend, installment_id_value=iid
    )
    backend.run_until(overdue_at)
    assert persistence.entity("loan_installment", iid).state == "overdue"
    assert persistence.entity(
        "delinquency_case", delinquency_case_id(loan.id, iid)
    ) is None

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    first = ensure_delinquency(
        persistence, rebuilt.engine, installment_id_value=iid
    )
    second = ensure_delinquency(
        persistence, rebuilt.engine, installment_id_value=iid
    )
    assert first.id == second.id == delinquency_case_id(loan.id, iid)
    assert persistence.entity("loan", loan.id).state == "delinquent"


def test_collection_followup_is_restart_equivalent():
    continuous, _, c_engine, c_backend, c_loan, c_due_dates = _prepare_servicing()
    c_iid = installment_id(c_loan.id, 1)
    c_backend.run_until(c_due_dates[0])
    c_overdue = schedule_overdue(
        continuous, c_engine, c_backend, installment_id_value=c_iid
    )
    c_backend.run_until(c_overdue)
    c_del = ensure_delinquency(continuous, c_engine, installment_id_value=c_iid)
    assert reconcile_collection(
        continuous, c_engine, c_backend, installment_id_value=c_iid, promise=True
    )
    c_case = continuous.entity(
        "loan_collection_case", collection_case_id(c_del.id)
    )
    c_followup = c_engine.scheduler.find_pending(
        entity_type="loan_collection_case",
        entity_id=c_case.id,
        name="escalate",
    )
    c_backend.run_until(c_followup.work.due_at)

    restarted, _, r_engine, r_backend, r_loan, r_due_dates = _prepare_servicing()
    r_iid = installment_id(r_loan.id, 1)
    r_backend.run_until(r_due_dates[0])
    r_overdue = schedule_overdue(
        restarted, r_engine, r_backend, installment_id_value=r_iid
    )
    r_backend.run_until(r_overdue)
    r_del = ensure_delinquency(restarted, r_engine, installment_id_value=r_iid)
    assert reconcile_collection(
        restarted, r_engine, r_backend, installment_id_value=r_iid, promise=True
    )
    r_case = restarted.entity(
        "loan_collection_case", collection_case_id(r_del.id)
    )
    r_followup = r_engine.scheduler.find_pending(
        entity_type="loan_collection_case",
        entity_id=r_case.id,
        name="escalate",
    )

    rebuilt = restart_reference_runtime(
        restarted,
        build_runtime,
        r_backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(r_followup.work.due_at)

    assert continuous.entity(
        "loan_collection_case", c_case.id
    ).state == restarted.entity(
        "loan_collection_case", r_case.id
    ).state == "escalated"


def test_approved_application_reconciles_missing_credit_decision_after_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    application = persistence.entity("loan_application", entities.application_id)
    command = engine.context.commands.create(
        "start_analysis",
        target=application,
        correlation_id=flow_correlation_id(entities.application_id),
        key=("credit-underwriting-crash", application.id, "start"),
    )
    engine.dispatch(command)
    application = persistence.entity("loan_application", entities.application_id)
    command = engine.context.commands.create(
        "approve",
        target=application,
        correlation_id=flow_correlation_id(entities.application_id),
        key=("credit-underwriting-crash", application.id, "approve"),
    )
    engine.dispatch(command)

    assert persistence.entity("loan_application", entities.application_id).state == "approved"
    assert persistence.entity(
        "credit_decision",
        credit_decision_id(entities.application_id),
    ) is None

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    assert reconcile_underwriting(
        persistence,
        rebuilt.engine,
        rebuilt.backend,
        entities=entities,
        approve=True,
    )
    decision_id = credit_decision_id(entities.application_id)
    assert persistence.entity("credit_decision", decision_id).state == "approved"


def test_initiated_payment_is_posted_then_applied_once_after_restart():
    persistence, entities, engine, backend, loan, due_dates = _prepare_servicing()
    iid = installment_id(loan.id, 1)
    backend.run_until(due_dates[0])

    payment = engine.context.entities.create(
        Payment,
        key=("credit-loans-reference", iid, "payment", 1),
        state="initiated",
        attributes={
            "loan_id": loan.id,
            "installment_id": iid,
            "ordinal": 1,
            "amount": 100.0,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(payment)

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    post_payment(
        persistence,
        rebuilt.engine,
        installment_id_value=iid,
        payment_ordinal=1,
        amount=100.0,
    )

    assert persistence.entity("loan_payment", payment.id).state == "posted"
    assert persistence.entity("loan_installment", iid).attributes["paid_amount"] == 100.0
    assert persistence.entity("loan", loan.id).attributes["outstanding_balance"] == 1100.0
