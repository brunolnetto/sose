from __future__ import annotations

import math

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.insurance.scenarios import ORIGIN
from sose.examples.insurance.simulation import (
    _entity,
    _reference_minor_units,
    build_runtime,
    claim_next_for_assessment,
    complete_assessment,
    ensure_document_request,
    ensure_fraud_investigation,
    ensure_payment,
    ensure_reserve,
    queue_claim,
    reconcile_fraud,
    reconcile_payment,
    reopen_claim,
    satisfy_documents,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(**seed_kwargs):
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, **seed_kwargs)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def _claim(persistence, entities):
    value = persistence.entity("insurance_claim", entities.claim_id)
    assert value is not None
    return value


def _policy(persistence, entities):
    value = persistence.entity("insurance_policy", entities.policy_id)
    assert value is not None
    return value


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_reference_minor_units_requires_finite_amount(value):
    with pytest.raises(ValueError, match="must be finite"):
        _reference_minor_units(value)


def test_reference_minor_units_rejects_fractional_minor_units():
    with pytest.raises(ValueError, match="fractional minor units"):
        _reference_minor_units(1.005)


@pytest.mark.parametrize("amount", [0.0, -1.0])
def test_seed_reference_requires_positive_amount(amount):
    with pytest.raises(ValueError, match="amount must be positive"):
        seed_reference(MemoryPersistence(), amount=amount)


def test_seed_reference_requires_currency():
    with pytest.raises(ValueError, match="currency must be non-empty"):
        seed_reference(MemoryPersistence(), currency="")


def test_missing_insurance_entity_guard_is_observable():
    with pytest.raises(RuntimeError, match="was not persisted"):
        _entity(MemoryPersistence(), "insurance_claim", "missing")


def test_document_request_requires_active_policy():
    persistence, entities, engine, backend = _runtime()
    policy = _policy(persistence, entities)
    policy.state = "lapsed"
    _save(persistence, policy)

    with pytest.raises(RuntimeError, match="active policy coverage"):
        ensure_document_request(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_document_request_rejects_unrelated_claim_state():
    persistence, entities, engine, backend = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "assessing"
    _save(persistence, claim)

    with pytest.raises(RuntimeError, match="opened/reopened claim"):
        ensure_document_request(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_expired_document_request_cannot_be_satisfied():
    persistence, entities, engine, backend = _runtime()
    request = ensure_document_request(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    request.state = "expired"
    _save(persistence, request)

    with pytest.raises(RuntimeError, match="cannot be satisfied"):
        satisfy_documents(
            persistence,
            engine,
            entities=entities,
        )


def test_claim_queue_requires_ready_or_reopened_claim():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="ready/reopened claim"):
        queue_claim(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_claim_next_withdraws_adjuster_request_when_scenario_unavailable(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="claims_adjuster",
        request_id="claims-adjuster:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None
    pending = engine.resources.ensure_requested(
        backend,
        resource_name="claims_adjuster",
        request_id="claims-adjuster:w1",
        requested_at=backend.now,
    )
    assert pending is None
    assert any(
        demand.request_id == "claims-adjuster:w1"
        for demand in persistence.resource_demands()
    )

    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "insurance.adjuster.available"
        else default,
    )

    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="w1",
    ) is None
    assert all(
        demand.request_id != "claims-adjuster:w1"
        for demand in persistence.resource_demands()
    )


def test_claim_next_releases_adjuster_when_queue_is_empty():
    persistence, entities, engine, backend = _runtime()

    assert claim_next_for_assessment(
        persistence,
        engine,
        backend,
        worker_id="w1",
    ) is None
    assert all(
        reservation.request_id != "claims-adjuster:w1"
        for reservation in persistence.resource_reservations()
    )


def test_complete_assessment_rejects_unknown_outcome():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(ValueError, match="unsupported assessment outcome"):
        complete_assessment(
            persistence,
            engine,
            backend,
            entities=entities,
            worker_id="w1",
            outcome="unknown",
        )


def test_complete_assessment_requires_active_assessment():
    persistence, entities, engine, backend = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "assessing"
    _save(persistence, claim)

    from sose.examples.insurance.entities import Assessment
    from sose.examples.insurance.simulation import assessment_id

    assessment = engine.context.entities.create(
        Assessment,
        key=("insurance-reference", claim.id, "assessment", 1),
        state="pending",
        attributes={"claim_id": claim.id, "ordinal": 1},
    )
    assert assessment.id == assessment_id(claim.id, 1)
    _save(persistence, assessment)

    with pytest.raises(RuntimeError, match="requires active claim assessment"):
        complete_assessment(
            persistence,
            engine,
            backend,
            entities=entities,
            worker_id="w1",
        )


def test_complete_assessment_requires_claim_to_be_assessing():
    persistence, entities, engine, backend = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "ready_for_assessment"
    _save(persistence, claim)

    from sose.examples.insurance.entities import Assessment
    from sose.examples.insurance.simulation import assessment_id

    assessment = engine.context.entities.create(
        Assessment,
        key=("insurance-reference", claim.id, "assessment", 1),
        state="in_progress",
        attributes={"claim_id": claim.id, "ordinal": 1},
    )
    assert assessment.id == assessment_id(claim.id, 1)
    _save(persistence, assessment)

    with pytest.raises(RuntimeError, match="requires active claim assessment"):
        complete_assessment(
            persistence,
            engine,
            backend,
            entities=entities,
            worker_id="w1",
        )


def test_fraud_investigation_requires_fraud_review_claim():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="Claim\(fraud_review\)"):
        ensure_fraud_investigation(
            persistence,
            engine,
            entities=entities,
        )


def test_fraud_reconciliation_waits_when_investigator_is_contended():
    persistence, entities, engine, backend = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "fraud_review"
    _save(persistence, claim)
    ensure_fraud_investigation(persistence, engine, entities=entities)

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="fraud_investigator",
        request_id="fraud:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_fraud(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_reserve_requires_approved_assessment_when_claim_is_approved():
    persistence, entities, engine, _ = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "approved"
    _save(persistence, claim)

    with pytest.raises(RuntimeError, match="approved claim and assessment"):
        ensure_reserve(
            persistence,
            engine,
            entities=entities,
        )


def test_reserve_requires_approved_claim_even_with_approved_assessment():
    persistence, entities, engine, _ = _runtime()
    claim = _claim(persistence, entities)

    from sose.examples.insurance.entities import Assessment
    from sose.examples.insurance.simulation import assessment_id

    assessment = engine.context.entities.create(
        Assessment,
        key=("insurance-reference", claim.id, "assessment", 1),
        state="approved",
        attributes={"claim_id": claim.id, "ordinal": 1},
    )
    assert assessment.id == assessment_id(claim.id, 1)
    _save(persistence, assessment)

    with pytest.raises(RuntimeError, match="approved claim and assessment"):
        ensure_reserve(
            persistence,
            engine,
            entities=entities,
        )


def test_reserve_requires_positive_amount_even_with_approved_evidence():
    persistence, entities, engine, _ = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "approved"
    _save(persistence, claim)

    from sose.examples.insurance.entities import Assessment
    from sose.examples.insurance.simulation import assessment_id

    assessment = engine.context.entities.create(
        Assessment,
        key=("insurance-reference", claim.id, "assessment", 1),
        state="approved",
        attributes={"claim_id": claim.id, "ordinal": 1},
    )
    assert assessment.id == assessment_id(claim.id, 1)
    _save(persistence, assessment)

    with pytest.raises(ValueError, match="reserve amount must be positive"):
        ensure_reserve(
            persistence,
            engine,
            entities=entities,
            amount=0.0,
        )


def test_payment_requires_established_reserve_when_claim_is_approved():
    persistence, entities, engine, _ = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "approved"
    _save(persistence, claim)

    with pytest.raises(RuntimeError, match="established Reserve"):
        ensure_payment(
            persistence,
            engine,
            entities=entities,
        )


def test_payment_requires_approved_claim_even_with_established_reserve():
    persistence, entities, engine, _ = _runtime()
    claim = _claim(persistence, entities)

    from sose.examples.insurance.entities import Reserve
    from sose.examples.insurance.simulation import reserve_id

    reserve = engine.context.entities.create(
        Reserve,
        key=("insurance-reference", claim.id, "reserve"),
        state="established",
        attributes={"claim_id": claim.id, "amount": 10.0, "currency": "USD"},
    )
    assert reserve.id == reserve_id(claim.id)
    _save(persistence, reserve)

    with pytest.raises(RuntimeError, match="established Reserve"):
        ensure_payment(
            persistence,
            engine,
            entities=entities,
        )


def test_payment_reconciliation_returns_false_for_not_due_payment():
    persistence, entities, engine, backend = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "approved"
    _save(persistence, claim)

    from sose.examples.insurance.entities import Reserve, Payment
    from sose.examples.insurance.simulation import reserve_id, payment_id

    reserve = engine.context.entities.create(
        Reserve,
        key=("insurance-reference", claim.id, "reserve"),
        state="established",
        attributes={"claim_id": claim.id, "amount": 10.0, "currency": "USD"},
    )
    assert reserve.id == reserve_id(claim.id)
    _save(persistence, reserve)
    payment = engine.context.entities.create(
        Payment,
        key=("insurance-reference", claim.id, "payment"),
        state="planned",
        attributes={
            "claim_id": claim.id,
            "amount": 10.0,
            "paid_amount": 0.0,
            "currency": "USD",
        },
    )
    assert payment.id == payment_id(claim.id)
    _save(persistence, payment)

    assert reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_payment_reconciliation_requires_established_reserve():
    persistence, entities, engine, backend = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "payment_scheduled"
    _save(persistence, claim)

    from sose.examples.insurance.entities import Reserve, Payment
    from sose.examples.insurance.simulation import reserve_id, payment_id

    reserve = engine.context.entities.create(
        Reserve,
        key=("insurance-reference", claim.id, "reserve"),
        state="released",
        attributes={"claim_id": claim.id, "amount": 10.0, "currency": "USD"},
    )
    assert reserve.id == reserve_id(claim.id)
    _save(persistence, reserve)
    payment = engine.context.entities.create(
        Payment,
        key=("insurance-reference", claim.id, "payment"),
        state="due",
        attributes={
            "claim_id": claim.id,
            "amount": 10.0,
            "paid_amount": 0.0,
            "currency": "USD",
        },
    )
    assert payment.id == payment_id(claim.id)
    _save(persistence, payment)

    with pytest.raises(RuntimeError, match="established reserve"):
        reconcile_payment(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_payment_reconciliation_respects_payment_outage(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "payment_scheduled"
    _save(persistence, claim)

    from sose.examples.insurance.entities import Reserve, Payment
    from sose.examples.insurance.simulation import reserve_id, payment_id

    reserve = engine.context.entities.create(
        Reserve,
        key=("insurance-reference", claim.id, "reserve"),
        state="established",
        attributes={"claim_id": claim.id, "amount": 10.0, "currency": "USD"},
    )
    assert reserve.id == reserve_id(claim.id)
    _save(persistence, reserve)
    payment = engine.context.entities.create(
        Payment,
        key=("insurance-reference", claim.id, "payment"),
        state="due",
        attributes={
            "claim_id": claim.id,
            "amount": 10.0,
            "paid_amount": 0.0,
            "currency": "USD",
        },
    )
    assert payment.id == payment_id(claim.id)
    _save(persistence, payment)

    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "insurance.payment.available"
        else default,
    )

    assert reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_partial_payment_requires_at_least_two_minor_units():
    persistence, entities, engine, backend = _runtime()
    claim = _claim(persistence, entities)
    claim.state = "payment_scheduled"
    _save(persistence, claim)

    from sose.examples.insurance.entities import Reserve, Payment
    from sose.examples.insurance.simulation import reserve_id, payment_id

    reserve = engine.context.entities.create(
        Reserve,
        key=("insurance-reference", claim.id, "reserve"),
        state="established",
        attributes={"claim_id": claim.id, "amount": 0.01, "currency": "USD"},
    )
    assert reserve.id == reserve_id(claim.id)
    _save(persistence, reserve)
    payment = engine.context.entities.create(
        Payment,
        key=("insurance-reference", claim.id, "payment"),
        state="due",
        attributes={
            "claim_id": claim.id,
            "amount": 0.01,
            "paid_amount": 0.0,
            "currency": "USD",
        },
    )
    assert payment.id == payment_id(claim.id)
    _save(persistence, payment)

    with pytest.raises(ValueError, match="at least two minor units"):
        reconcile_payment(
            persistence,
            engine,
            backend,
            entities=entities,
            partial=True,
        )


def test_reopen_claim_requires_rejected_state():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="only rejected claims"):
        reopen_claim(
            persistence,
            engine,
            entities=entities,
        )
