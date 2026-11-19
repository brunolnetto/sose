"""Insurance reserve and payment workflow (split from simulation.py)."""

from __future__ import annotations

from .entities import Payment, Reserve
from .foundation import (
    PAYMENT_DELAY,
    _claim,
    _dispatch,
    _entity,
    _reference_minor_units,
    assessment_id,
    flow_correlation_id,
    payment_id,
    reserve_id,
)


def ensure_reserve(persistence, engine, *, entities, amount=None):
    claim = _claim(persistence, entities)
    existing = persistence.entity("insurance_reserve", reserve_id(claim.id))
    if existing is not None:
        if existing.state == "proposed":
            _dispatch(
                engine,
                existing,
                "establish",
                key=("insurance-reserve", existing.id, "establish"),
                correlation_id=flow_correlation_id(claim.id),
            )
            existing = _entity(
                persistence,
                "insurance_reserve",
                existing.id,
            )
        return existing
    approved_assessment = _latest_approved_assessment(persistence, claim_id=claim.id)
    if claim.state != "approved" or approved_assessment is None:
        raise RuntimeError("reserve requires approved claim and assessment evidence")
    reserve_amount = _validated_reserve_amount(
        amount if amount is not None else claim.attributes["amount"]
    )
    value = engine.context.entities.create(
        Reserve,
        key=("insurance-reference", claim.id, "reserve"),
        state="proposed",
        attributes={
            "claim_id": claim.id,
            "amount": reserve_amount,
            "currency": claim.attributes["currency"],
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    _dispatch(
        engine,
        value,
        "establish",
        key=("insurance-reserve", value.id, "establish"),
        correlation_id=flow_correlation_id(claim.id),
    )
    return _entity(persistence, "insurance_reserve", value.id)


def ensure_payment(persistence, engine, *, entities):
    claim = _claim(persistence, entities)
    existing = persistence.entity("insurance_payment", payment_id(claim.id))
    if existing is not None:
        return existing
    reserve = persistence.entity("insurance_reserve", reserve_id(claim.id))
    if claim.state != "approved" or reserve is None or reserve.state != "established":
        raise RuntimeError("payment requires Claim(approved) and established Reserve")
    value = engine.context.entities.create(
        Payment,
        key=("insurance-reference", claim.id, "payment"),
        state="planned",
        attributes={
            "claim_id": claim.id,
            "amount": float(reserve.attributes["amount"]),
            "paid_amount": 0.0,
            "currency": reserve.attributes["currency"],
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    return value


def schedule_payment(
    persistence,
    engine,
    backend,
    *,
    entities,
    delay=PAYMENT_DELAY,
):
    claim = _claim(persistence, entities)
    payment = ensure_payment(persistence, engine, entities=entities)
    correlation_id = flow_correlation_id(claim.id)
    if payment.state == "planned":
        _dispatch(
            engine,
            payment,
            "schedule",
            key=("insurance-payment", payment.id, "schedule"),
            correlation_id=correlation_id,
        )
        payment = _entity(persistence, "insurance_payment", payment.id)
    claim = _claim(persistence, entities)
    if claim.state == "approved":
        _dispatch(
            engine,
            claim,
            "schedule_payment",
            key=("insurance", claim.id, payment.id, "schedule-payment"),
            correlation_id=correlation_id,
        )
    existing = engine.scheduler.find_pending(
        entity_type="insurance_payment",
        entity_id=payment.id,
        name="make_due",
    )
    if payment.state == "scheduled" and existing is None:
        due_at = backend.now + delay
        command = engine.context.commands.create(
            "make_due",
            target=payment,
            due_at=due_at,
            correlation_id=correlation_id,
            key=("insurance-payment", payment.id, "due"),
        )
        engine.context.schedules.at(due_at, command=command)
        return due_at
    if existing is not None:
        return existing.work.due_at
    return backend.now


def reconcile_payment(
    persistence,
    engine,
    backend,
    *,
    entities,
    partial=False,
):
    claim = _claim(persistence, entities)
    payment = _entity(
        persistence,
        "insurance_payment",
        payment_id(claim.id),
    )
    reserve = _entity(
        persistence,
        "insurance_reserve",
        reserve_id(claim.id),
    )
    request_id = f"payment-processor:{payment.id}"

    if payment.state == "paid":
        return _reconcile_paid_payment_terminal(
            engine,
            backend,
            claim=claim,
            payment=payment,
            reserve=reserve,
            request_id=request_id,
        )

    if payment.state not in {"due", "partially_paid"}:
        return False
    if reserve.state != "established":
        raise RuntimeError("payout requires established reserve")
    if not engine.context.scenarios.attribute("insurance.payment.available", True):
        engine.resources.withdraw(backend, request_id)
        return False

    partial_units = _partial_units_if_requested(payment=payment, partial=partial)

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="payment_processor",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(claim.id)
    payment = _entity(persistence, "insurance_payment", payment.id)
    if partial and payment.state == "due":
        assert partial_units is not None
        payment.attributes["paid_amount"] = (partial_units // 2) / 100
        with persistence.transaction() as uow:
            uow.save_entity(payment)
        _dispatch(
            engine,
            payment,
            "record_partial",
            key=("insurance-payment", payment.id, "partial"),
            correlation_id=correlation_id,
        )
        engine.resources.withdraw(backend, request_id)
        return False

    if payment.state in {"due", "partially_paid"}:
        _dispatch_payment_completion(
            persistence,
            engine,
            payment=payment,
            correlation_id=correlation_id,
        )

    engine.resources.withdraw(backend, request_id)
    return reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )


def _latest_approved_assessment(persistence, *, claim_id: str):
    return next(
        (
            candidate
            for ordinal in (3, 2, 1)
            for candidate in (
                persistence.entity(
                    "insurance_assessment",
                    assessment_id(claim_id, ordinal),
                ),
            )
            if candidate is not None and candidate.state == "approved"
        ),
        None,
    )


def _validated_reserve_amount(amount: float) -> float:
    reserve_units = _reference_minor_units(float(amount))
    if reserve_units <= 0:
        raise ValueError("reserve amount must be positive")
    return reserve_units / 100


def _reconcile_paid_payment_terminal(
    engine,
    backend,
    *,
    claim,
    payment,
    reserve,
    request_id: str,
) -> bool:
    engine.resources.withdraw(backend, request_id)
    if claim.state == "payment_scheduled":
        _dispatch(
            engine,
            claim,
            "record_payment",
            key=("insurance", claim.id, payment.id, "record-payment"),
            correlation_id=flow_correlation_id(claim.id),
        )
    if reserve.state == "established":
        _dispatch(
            engine,
            reserve,
            "release",
            key=("insurance-reserve", reserve.id, "release"),
            correlation_id=flow_correlation_id(claim.id),
        )
    return True


def _partial_units_if_requested(*, payment, partial: bool) -> int | None:
    if not partial or payment.state != "due":
        return None
    partial_units = _reference_minor_units(float(payment.attributes["amount"]))
    if partial_units < 2:
        raise ValueError("partial payout requires at least two minor units")
    return partial_units


def _dispatch_payment_completion(
    persistence,
    engine,
    *,
    payment,
    correlation_id: str,
) -> None:
    payment.attributes["paid_amount"] = float(payment.attributes["amount"])
    with persistence.transaction() as uow:
        uow.save_entity(payment)
    _dispatch(
        engine,
        payment,
        "complete",
        key=("insurance-payment", payment.id, "complete"),
        correlation_id=correlation_id,
    )
