"""Credit-loan payment posting and balance application (split from simulation.py)."""

from __future__ import annotations

from .entities import (
    Payment,
)
from .simulation_runtime import (
    _dispatch,
    _entity,
    _usd_cents,
    flow_correlation_id,
    payment_id,
)


def _validate_payment_against_installment(installment, payment) -> None:
    payable_states = {"due", "partially_paid", "overdue"}
    if payment is None and installment.state not in payable_states:
        raise RuntimeError(
            f"new payment requires payable installment, got {installment.state}"
        )
    if (
        payment is not None
        and payment.state == "initiated"
        and installment.state not in payable_states
    ):
        raise RuntimeError(
            f"initiated payment cannot post against {installment.state} installment"
        )


def _ensure_posted_payment(
    persistence,
    engine,
    *,
    installment,
    loan,
    payment_ordinal: int,
    amount: float,
    correlation_id: str,
):
    pid = payment_id(installment.id, payment_ordinal)
    payment = persistence.entity("loan_payment", pid)
    _validate_payment_against_installment(installment, payment)

    if payment is None:
        remaining = float(installment.attributes["amount"]) - float(
            installment.attributes["paid_amount"]
        )
        if amount > remaining + 1e-9:
            raise ValueError("payment exceeds installment remaining balance")
        payment = engine.context.entities.create(
            Payment,
            key=("credit-loans-reference", installment.id, "payment", payment_ordinal),
            state="initiated",
            attributes={
                "loan_id": loan.id,
                "installment_id": installment.id,
                "ordinal": payment_ordinal,
                "amount": float(amount),
                "currency": loan.attributes["currency"],
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(payment)
    elif abs(float(payment.attributes["amount"]) - float(amount)) > 1e-9:
        raise ValueError("payment identity already exists with a different amount")

    if payment.state == "initiated":
        _dispatch(
            engine,
            payment,
            "post",
            key=("credit-payment", payment.id, "post"),
            correlation_id=correlation_id,
        )
        payment = _entity(persistence, "loan_payment", payment.id)
    if payment.state != "posted":
        raise RuntimeError(f"payment cannot be applied from {payment.state}")
    return payment


def _linked_replacement_installment(persistence, installment):
    if installment.state != "restructured":
        return None
    restructure_id_value = installment.attributes.get("superseded_by_restructure_id")
    if restructure_id_value is None:
        return None
    restructure = _entity(
        persistence,
        "loan_restructure",
        str(restructure_id_value),
    )
    replacement_id = restructure.attributes.get("replacement_installment_id")
    if replacement_id is None:
        return None
    return _entity(
        persistence,
        "loan_installment",
        str(replacement_id),
    )


def _apply_payment_to_balances(
    persistence,
    *,
    installment,
    loan,
    payment,
):
    applied = list(installment.attributes.get("applied_payment_ids", []))
    if payment.id in applied:
        return None, False

    installment.attributes["paid_amount"] = round(
        float(installment.attributes["paid_amount"]) + float(payment.attributes["amount"]),
        2,
    )
    applied.append(payment.id)
    installment.attributes["applied_payment_ids"] = applied

    loan_applied = list(loan.attributes.get("applied_payment_ids", []))
    if payment.id not in loan_applied:
        loan.attributes["outstanding_balance"] = round(
            max(
                0.0,
                float(loan.attributes["outstanding_balance"])
                - float(payment.attributes["amount"]),
            ),
            2,
        )
        loan_applied.append(payment.id)
        loan.attributes["applied_payment_ids"] = loan_applied

    replacement = _linked_replacement_installment(persistence, installment)
    if replacement is not None:
        replacement.attributes["amount"] = round(
            max(
                0.0,
                float(replacement.attributes["amount"])
                - float(payment.attributes["amount"]),
            ),
            2,
        )
    return replacement, True


def _dispatch_installment_payment_progress(
    engine,
    installment,
    payment,
    *,
    correlation_id: str,
) -> None:
    paid = float(installment.attributes["paid_amount"])
    required = float(installment.attributes["amount"])
    if paid + 1e-9 >= required:
        if installment.state in {"due", "partially_paid", "overdue"}:
            _dispatch(
                engine,
                installment,
                "complete",
                key=("credit-installment", installment.id, "complete"),
                correlation_id=correlation_id,
            )
        engine.scheduler.cancel_pending(
            entity_type="loan_installment",
            entity_id=installment.id,
            name="miss",
        )
        return

    if installment.state in {"due", "overdue"}:
        _dispatch(
            engine,
            installment,
            "record_partial",
            key=("credit-installment", installment.id, "partial", payment.id),
            correlation_id=correlation_id,
        )


def _dispatch_loan_payoff_if_eligible(engine, loan, *, correlation_id: str) -> None:
    if float(loan.attributes["outstanding_balance"]) > 1e-9:
        return
    if loan.state not in {"servicing", "delinquent"}:
        return
    _dispatch(
        engine,
        loan,
        "pay_off",
        key=("credit-loans", loan.id, "pay-off"),
        correlation_id=correlation_id,
    )


def post_payment(
    persistence,
    engine,
    *,
    installment_id_value,
    payment_ordinal: int,
    amount: float,
):
    amount_cents = _usd_cents(amount)
    if amount_cents <= 0:
        raise ValueError("payment amount must be positive")
    amount = amount_cents / 100
    installment = _entity(persistence, "loan_installment", installment_id_value)
    loan = _entity(persistence, "loan", installment.attributes["loan_id"])
    correlation_id = flow_correlation_id(str(loan.attributes["application_id"]))
    payment = _ensure_posted_payment(
        persistence,
        engine,
        installment=installment,
        loan=loan,
        payment_ordinal=payment_ordinal,
        amount=amount,
        correlation_id=correlation_id,
    )

    installment = _entity(persistence, "loan_installment", installment.id)
    loan = _entity(persistence, "loan", loan.id)
    replacement, changed = _apply_payment_to_balances(
        persistence,
        installment=installment,
        loan=loan,
        payment=payment,
    )
    if changed:
        with persistence.transaction() as uow:
            uow.save_entity(installment)
            uow.save_entity(loan)
            if replacement is not None:
                uow.save_entity(replacement)

    installment = _entity(persistence, "loan_installment", installment.id)
    _dispatch_installment_payment_progress(
        engine,
        installment,
        payment,
        correlation_id=correlation_id,
    )

    loan = _entity(persistence, "loan", loan.id)
    _dispatch_loan_payoff_if_eligible(
        engine,
        loan,
        correlation_id=correlation_id,
    )
    return _entity(persistence, "loan_payment", payment.id)
