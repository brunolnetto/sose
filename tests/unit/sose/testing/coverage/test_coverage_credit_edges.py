from __future__ import annotations

import math

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.credit_loans.entities import Payment
from sose.examples.credit_loans import simulation as credit
from sose.persistence.memory import MemoryPersistence


def _approved_schedule(*, principal: float = 1200.0, installment_count: int = 3):
    persistence = MemoryPersistence()
    entities = credit.seed_reference(
        persistence,
        principal=principal,
        installment_count=installment_count,
    )
    _, engine = credit.build_runtime(persistence)
    backend = SimPyBackend(origin=credit.ORIGIN)
    engine.rebuild_backend(backend)
    assert credit.reconcile_underwriting(
        persistence,
        engine,
        backend,
        entities=entities,
        approve=True,
    )
    due_dates = credit.disburse_and_schedule(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    loan = persistence.entity("loan", credit.loan_id(entities.application_id))
    assert loan is not None
    return persistence, entities, engine, backend, loan, due_dates


def test_credit_money_and_seed_validation_edges():
    with pytest.raises(ValueError, match="USD amount must be finite"):
        credit._usd_cents(math.inf)
    with pytest.raises(ValueError, match="principal must be positive"):
        credit.seed_reference(MemoryPersistence(), principal=0.0)
    with pytest.raises(ValueError, match="installment_count must be positive"):
        credit.seed_reference(MemoryPersistence(), installment_count=0)


def test_credit_entity_lookup_fails_loudly_for_missing_durable_state():
    with pytest.raises(RuntimeError, match="loan was not persisted: missing"):
        credit._entity(MemoryPersistence(), "loan", "missing")


def test_underwriting_returns_pending_when_credit_analyst_is_busy():
    persistence = MemoryPersistence()
    entities = credit.seed_reference(persistence)
    _, engine = credit.build_runtime(persistence)
    backend = SimPyBackend(origin=credit.ORIGIN)
    engine.rebuild_backend(backend)

    engine.resources.request(
        backend,
        resource_name="credit_analyst",
        request_id="analyst-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert credit.reconcile_underwriting(
        persistence,
        engine,
        backend,
        entities=entities,
        approve=True,
    ) is False


def test_loan_creation_rejects_rejected_credit_decision():
    persistence = MemoryPersistence()
    entities = credit.seed_reference(persistence)
    _, engine = credit.build_runtime(persistence)
    backend = SimPyBackend(origin=credit.ORIGIN)
    engine.rebuild_backend(backend)

    assert credit.reconcile_underwriting(
        persistence,
        engine,
        backend,
        entities=entities,
        approve=False,
    ) is False

    with pytest.raises(
        RuntimeError,
        match="loan creation requires approved application and credit decision",
    ):
        credit.ensure_loan(persistence, engine, entities=entities)


def test_disbursement_reschedule_uses_now_for_already_due_installment():
    persistence, entities, engine, backend, loan, due_dates = _approved_schedule()

    first_id = credit.installment_id(loan.id, 1)
    backend.run_until(due_dates[0])
    assert persistence.entity("loan_installment", first_id).state == "due"

    replay_dates = credit.disburse_and_schedule(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    assert replay_dates[0] == backend.now
    assert replay_dates[1:] == due_dates[1:]


def test_schedule_overdue_is_noop_before_due_and_idempotent_once_pending():
    persistence, _, engine, backend, loan, due_dates = _approved_schedule()
    first_id = credit.installment_id(loan.id, 1)

    assert (
        credit.schedule_overdue(
            persistence,
            engine,
            backend,
            installment_id_value=first_id,
        )
        == backend.now
    )

    backend.run_until(due_dates[0])
    first_overdue = credit.schedule_overdue(
        persistence,
        engine,
        backend,
        installment_id_value=first_id,
    )
    assert credit.schedule_overdue(
        persistence,
        engine,
        backend,
        installment_id_value=first_id,
    ) == first_overdue


def test_payment_rejects_non_positive_and_overpayment():
    persistence, _, engine, backend, loan, due_dates = _approved_schedule()
    first_id = credit.installment_id(loan.id, 1)
    backend.run_until(due_dates[0])

    with pytest.raises(ValueError, match="payment amount must be positive"):
        credit.post_payment(
            persistence,
            engine,
            installment_id_value=first_id,
            payment_ordinal=1,
            amount=0.0,
        )

    installment = persistence.entity("loan_installment", first_id)
    assert installment is not None
    with pytest.raises(ValueError, match="payment exceeds installment remaining balance"):
        credit.post_payment(
            persistence,
            engine,
            installment_id_value=first_id,
            payment_ordinal=1,
            amount=float(installment.attributes["amount"]) + 0.01,
        )


def test_payment_identity_rejects_different_amount_on_retry():
    persistence, _, engine, backend, loan, due_dates = _approved_schedule()
    first_id = credit.installment_id(loan.id, 1)
    backend.run_until(due_dates[0])

    credit.post_payment(
        persistence,
        engine,
        installment_id_value=first_id,
        payment_ordinal=1,
        amount=100.0,
    )
    with pytest.raises(
        ValueError,
        match="payment identity already exists with a different amount",
    ):
        credit.post_payment(
            persistence,
            engine,
            installment_id_value=first_id,
            payment_ordinal=1,
            amount=99.0,
        )


def _save_payment(
    persistence,
    *,
    installment_id_value: str,
    loan_id_value: str,
    ordinal: int,
    amount: float,
    state: str,
):
    payment = Payment(
        id=credit.payment_id(installment_id_value, ordinal),
        state=state,
        version=1,
        attributes={
            "loan_id": loan_id_value,
            "installment_id": installment_id_value,
            "ordinal": ordinal,
            "amount": amount,
            "currency": "USD",
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(payment)
    return payment


def test_initiated_payment_cannot_post_against_scheduled_installment():
    persistence, _, engine, _, loan, _ = _approved_schedule()
    second_id = credit.installment_id(loan.id, 2)
    installment = persistence.entity("loan_installment", second_id)
    assert installment is not None and installment.state == "scheduled"
    amount = float(installment.attributes["amount"])

    _save_payment(
        persistence,
        installment_id_value=second_id,
        loan_id_value=loan.id,
        ordinal=99,
        amount=amount,
        state="initiated",
    )

    with pytest.raises(RuntimeError, match="initiated payment cannot post against scheduled installment"):
        credit.post_payment(
            persistence,
            engine,
            installment_id_value=second_id,
            payment_ordinal=99,
            amount=amount,
        )


def test_failed_payment_cannot_be_applied_to_due_installment():
    persistence, _, engine, backend, loan, due_dates = _approved_schedule()
    first_id = credit.installment_id(loan.id, 1)
    backend.run_until(due_dates[0])
    installment = persistence.entity("loan_installment", first_id)
    assert installment is not None
    amount = float(installment.attributes["amount"])

    _save_payment(
        persistence,
        installment_id_value=first_id,
        loan_id_value=loan.id,
        ordinal=99,
        amount=amount,
        state="failed",
    )

    with pytest.raises(RuntimeError, match="payment cannot be applied from failed"):
        credit.post_payment(
            persistence,
            engine,
            installment_id_value=first_id,
            payment_ordinal=99,
            amount=amount,
        )


def test_delinquency_requires_overdue_installment():
    persistence, _, engine, backend, loan, due_dates = _approved_schedule()
    first_id = credit.installment_id(loan.id, 1)
    backend.run_until(due_dates[0])

    with pytest.raises(RuntimeError, match="delinquency requires overdue installment"):
        credit.ensure_delinquency(
            persistence,
            engine,
            installment_id_value=first_id,
        )
