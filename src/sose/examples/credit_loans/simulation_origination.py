"""Credit-loan underwriting, origination and disbursement (split from simulation.py)."""

from __future__ import annotations

from datetime import timedelta

from .entities import (
    CreditDecision,
    Installment,
    Loan,
)
from .simulation_runtime import (
    FIRST_DUE_DELAY,
    INSTALLMENT_INTERVAL,
    OVERDUE_GRACE,
    _dispatch,
    _entity,
    _usd_cents,
    credit_decision_id,
    flow_correlation_id,
    installment_id,
    loan_id,
)


def _ensure_credit_decision(
    persistence,
    engine,
    *,
    application,
) -> CreditDecision:
    decision = persistence.entity("credit_decision", credit_decision_id(application.id))
    if decision is not None:
        return decision
    decision = engine.context.entities.create(
        CreditDecision,
        key=("credit-loans-reference", application.id, "decision"),
        state="pending",
        attributes={"application_id": application.id},
    )
    with persistence.transaction() as uow:
        uow.save_entity(decision)
    return decision


def _reconcile_final_underwriting_state(
    persistence,
    engine,
    backend,
    *,
    application,
    correlation_id: str,
    request_id: str,
) -> bool:
    expected_state = application.state
    expected_event = "approve" if expected_state == "approved" else "reject"
    decision = _ensure_credit_decision(
        persistence,
        engine,
        application=application,
    )
    decision = _entity(persistence, "credit_decision", decision.id)
    if decision.state == "pending":
        _dispatch(
            engine,
            decision,
            expected_event,
            key=("credit-decision", decision.id, expected_event),
            correlation_id=correlation_id,
        )
    engine.resources.withdraw(backend, request_id)
    return expected_state == "approved"


def _ensure_underwriter_capacity(
    engine,
    backend,
    *,
    request_id: str,
) -> bool:
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="credit_analyst",
        request_id=request_id,
        requested_at=backend.now,
    )
    return reservation is not None


def _dispatch_underwriting_decisions(
    persistence,
    engine,
    *,
    application,
    decision: CreditDecision,
    approve: bool,
    correlation_id: str,
) -> None:
    event = "approve" if approve else "reject"
    if decision.state == "pending":
        _dispatch(
            engine,
            decision,
            event,
            key=("credit-decision", decision.id, event),
            correlation_id=correlation_id,
        )
    if application.state == "under_analysis":
        _dispatch(
            engine,
            application,
            event,
            key=("credit-loans", application.id, event),
            correlation_id=correlation_id,
        )


def reconcile_underwriting(
    persistence,
    engine,
    backend,
    *,
    entities,
    approve: bool,
):
    application = _entity(persistence, "loan_application", entities.application_id)
    correlation_id = flow_correlation_id(application.id)
    request_id = f"credit-analyst:{application.id}"

    if application.state in {"approved", "rejected"}:
        return _reconcile_final_underwriting_state(
            persistence,
            engine,
            backend,
            application=application,
            correlation_id=correlation_id,
            request_id=request_id,
        )

    if not engine.context.scenarios.attribute("credit_loans.underwriting.available", True):
        engine.resources.withdraw(backend, request_id)
        return False

    if not _ensure_underwriter_capacity(
        engine,
        backend,
        request_id=request_id,
    ):
        return False

    application = _entity(persistence, "loan_application", application.id)
    if application.state == "submitted":
        _dispatch(
            engine,
            application,
            "start_analysis",
            key=("credit-loans", application.id, "start-analysis"),
            correlation_id=correlation_id,
        )

    decision = _ensure_credit_decision(
        persistence,
        engine,
        application=application,
    )
    application = _entity(persistence, "loan_application", application.id)
    decision = _entity(persistence, "credit_decision", decision.id)
    _dispatch_underwriting_decisions(
        persistence,
        engine,
        application=application,
        decision=decision,
        approve=approve,
        correlation_id=correlation_id,
    )

    engine.resources.withdraw(backend, request_id)
    return _entity(persistence, "loan_application", application.id).state == "approved"


def ensure_loan(persistence, engine, *, entities):
    existing = persistence.entity("loan", loan_id(entities.application_id))
    if existing is not None:
        return existing
    application = _entity(persistence, "loan_application", entities.application_id)
    decision = _entity(persistence, "credit_decision", credit_decision_id(application.id))
    if application.state != "approved" or decision.state != "approved":
        raise RuntimeError("loan creation requires approved application and credit decision")
    value = engine.context.entities.create(
        Loan,
        key=("credit-loans-reference", application.id, "loan"),
        state="approved",
        attributes={
            "application_id": application.id,
            "principal": float(application.attributes["principal"]),
            "outstanding_balance": float(application.attributes["principal"]),
            "currency": application.attributes["currency"],
            "installment_count": int(application.attributes["installment_count"]),
            "applied_payment_ids": [],
            "delinquency_case_ids": [],
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    return value


def disburse_and_schedule(
    persistence,
    engine,
    backend,
    *,
    entities,
    first_due_delay: timedelta = FIRST_DUE_DELAY,
    installment_interval: timedelta = INSTALLMENT_INTERVAL,
):
    loan = ensure_loan(persistence, engine, entities=entities)
    correlation_id = flow_correlation_id(entities.application_id)
    if loan.state == "approved":
        _dispatch(
            engine,
            loan,
            "disburse",
            key=("credit-loans", loan.id, "disburse"),
            correlation_id=correlation_id,
        )
        loan = _entity(persistence, "loan", loan.id)
    if loan.state == "disbursed":
        _dispatch(
            engine,
            loan,
            "activate",
            key=("credit-loans", loan.id, "activate"),
            correlation_id=correlation_id,
        )

    count = int(loan.attributes["installment_count"])
    principal_cents = _usd_cents(float(loan.attributes["principal"]))
    base_cents = principal_cents // count
    due_dates = []
    for ordinal in range(1, count + 1):
        amount_cents = (
            base_cents
            if ordinal < count
            else principal_cents - base_cents * (count - 1)
        )
        amount = amount_cents / 100
        iid = installment_id(loan.id, ordinal)
        installment = persistence.entity("loan_installment", iid)
        if installment is None:
            installment = engine.context.entities.create(
                Installment,
                key=("credit-loans-reference", loan.id, "installment", ordinal),
                state="scheduled",
                attributes={
                    "loan_id": loan.id,
                    "ordinal": ordinal,
                    "amount": amount,
                    "paid_amount": 0.0,
                    "currency": loan.attributes["currency"],
                    "applied_payment_ids": [],
                },
            )
            with persistence.transaction() as uow:
                uow.save_entity(installment)
        existing = engine.scheduler.find_pending(
            entity_type="loan_installment",
            entity_id=installment.id,
            name="make_due",
        )
        if installment.state == "scheduled" and existing is None:
            due_at = backend.now + first_due_delay + installment_interval * (ordinal - 1)
            command = engine.context.commands.create(
                "make_due",
                target=installment,
                due_at=due_at,
                correlation_id=correlation_id,
                key=("credit-loans", installment.id, "make-due"),
            )
            engine.context.schedules.at(due_at, command=command)
        else:
            due_at = backend.now if existing is None else existing.work.due_at
        due_dates.append(due_at)
    return tuple(due_dates)


def schedule_overdue(persistence, engine, backend, *, installment_id_value, delay=OVERDUE_GRACE):
    installment = _entity(persistence, "loan_installment", installment_id_value)
    if installment.state not in {"due", "partially_paid"}:
        return backend.now
    existing = engine.scheduler.find_pending(
        entity_type="loan_installment",
        entity_id=installment.id,
        name="miss",
    )
    if existing is not None:
        return existing.work.due_at
    due_at = backend.now + delay
    command = engine.context.commands.create(
        "miss",
        target=installment,
        due_at=due_at,
        correlation_id=flow_correlation_id(
            str(_entity(persistence, "loan", installment.attributes["loan_id"]).attributes["application_id"])
        ),
        key=("credit-loans", installment.id, "miss"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at
