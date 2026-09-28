from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import (
    Assessment,
    Claim,
    DocumentRequest,
    FraudInvestigation,
    Payment,
    Policy,
    Reserve,
)
from .scenarios import ORIGIN
from .statecharts import (
    AssessmentChart,
    ClaimChart,
    DocumentRequestChart,
    FraudInvestigationChart,
    PaymentChart,
    PolicyChart,
    ReserveChart,
)


DOCUMENT_DEADLINE = timedelta(hours=4)
PAYMENT_DELAY = timedelta(hours=2)


def _reference_minor_units(value: float) -> int:
    scaled = float(value) * 100
    if not math.isfinite(scaled):
        raise ValueError("Reference payout amount must be finite")

    # Float inputs are accepted only while their representation is precise
    # enough to distinguish a cent from a true sub-cent amount. Within that
    # envelope, tolerate ordinary binary noise around the nearest cent.
    ulp = math.ulp(scaled)
    if ulp > 1e-4:
        raise ValueError("Reference payout amount exceeds cent-safe float precision")
    nearest = round(scaled)
    tolerance = max(1e-7, 2 * ulp)
    if abs(scaled - nearest) > tolerance:
        raise ValueError("Reference payout amount must not contain fractional minor units")
    return int(nearest)


@dataclass(frozen=True, slots=True)
class InsuranceEntities:
    policy_id: str
    claim_id: str


def flow_correlation_id(claim_id: str) -> str:
    return deterministic_id("insurance-flow", claim_id)


def document_request_id(claim_id: str, ordinal: int = 1) -> str:
    return deterministic_id(
        "entity", "insurance_document_request",
        "insurance-reference", claim_id, "document-request", ordinal,
    )


def assessment_id(claim_id: str, ordinal: int = 1) -> str:
    return deterministic_id(
        "entity", "insurance_assessment",
        "insurance-reference", claim_id, "assessment", ordinal,
    )


def fraud_investigation_id(claim_id: str) -> str:
    return deterministic_id(
        "entity", "insurance_fraud_investigation",
        "insurance-reference", claim_id, "fraud-investigation",
    )


def reserve_id(claim_id: str) -> str:
    return deterministic_id(
        "entity", "insurance_reserve",
        "insurance-reference", claim_id, "reserve",
    )


def payment_id(claim_id: str) -> str:
    return deterministic_id(
        "entity", "insurance_payment",
        "insurance-reference", claim_id, "payment",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=511),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("insurance_policy", PolicyChart))
    registry.register(EntityType("insurance_claim", ClaimChart))
    registry.register(EntityType("insurance_document_request", DocumentRequestChart))
    registry.register(EntityType("insurance_assessment", AssessmentChart))
    registry.register(EntityType("insurance_reserve", ReserveChart))
    registry.register(EntityType("insurance_payment", PaymentChart))
    registry.register(
        EntityType("insurance_fraud_investigation", FraudInvestigationChart)
    )
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    severity: int = 50,
    amount: float = 5000.0,
    claim_type: str = "property",
    currency: str = "USD",
) -> InsuranceEntities:
    amount_units = _reference_minor_units(amount)
    if amount_units <= 0:
        raise ValueError("amount must be positive")
    amount = amount_units / 100
    if not currency:
        raise ValueError("currency must be non-empty")
    context, engine = build_runtime(persistence)
    policy = context.entities.create(
        Policy,
        key=("insurance-reference", "policy-1"),
        state="active",
        attributes={
            "coverage_limit": 10000.0,
            "currency": currency,
            "claim_type": claim_type,
        },
    )
    claim = context.entities.create(
        Claim,
        key=("insurance-reference", "claim-1"),
        state="opened",
        attributes={
            "policy_id": policy.id,
            "severity": int(severity),
            "amount": float(amount),
            "currency": currency,
            "claim_type": claim_type,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(policy)
        uow.save_entity(claim)
        uow.save_resource_definition(ResourceDefinition("claims_adjuster", capacity=1))
        uow.save_resource_definition(ResourceDefinition("fraud_investigator", capacity=1))
        uow.save_resource_definition(ResourceDefinition("payment_processor", capacity=1))
    engine.stores.define(StoreDefinition("claim_queue", kind="priority", capacity=100))
    return InsuranceEntities(policy_id=policy.id, claim_id=claim.id)


def _entity(persistence, entity_type, entity_id):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _policy(persistence, entities):
    return _entity(persistence, "insurance_policy", entities.policy_id)


def _claim(persistence, entities):
    return _entity(persistence, "insurance_claim", entities.claim_id)


def _dispatch(engine, entity, event, *, key, correlation_id):
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def ensure_document_request(
    persistence,
    engine,
    backend,
    *,
    entities,
    ordinal=1,
    deadline=DOCUMENT_DEADLINE,
):
    claim = _claim(persistence, entities)
    policy = _policy(persistence, entities)
    if policy.state != "active":
        raise RuntimeError("documentation requires active policy coverage")

    request = persistence.entity(
        "insurance_document_request",
        document_request_id(claim.id, ordinal),
    )
    correlation_id = flow_correlation_id(claim.id)
    if request is None:
        if claim.state in {"opened", "reopened"}:
            _dispatch(
                engine,
                claim,
                "request_documents",
                key=("insurance", claim.id, "request-documents", ordinal),
                correlation_id=correlation_id,
            )
        elif claim.state != "pending_documents":
            raise RuntimeError(
                f"document request requires opened/reopened claim, got {claim.state}"
            )
        request = engine.context.entities.create(
            DocumentRequest,
            key=("insurance-reference", claim.id, "document-request", ordinal),
            state="open",
            attributes={"claim_id": claim.id, "ordinal": ordinal},
        )
        with persistence.transaction() as uow:
            uow.save_entity(request)

    existing = engine.scheduler.find_pending(
        entity_type="insurance_document_request",
        entity_id=request.id,
        name="expire",
    )
    if request.state == "open" and existing is None:
        due_at = backend.now + deadline
        command = engine.context.commands.create(
            "expire",
            target=request,
            due_at=due_at,
            correlation_id=correlation_id,
            key=("insurance", request.id, "document-deadline"),
        )
        engine.context.schedules.at(due_at, command=command)
    return request


def satisfy_documents(persistence, engine, *, entities, ordinal=1):
    claim = _claim(persistence, entities)
    request = _entity(
        persistence,
        "insurance_document_request",
        document_request_id(claim.id, ordinal),
    )
    if request.state == "expired":
        raise RuntimeError("expired document request cannot be satisfied")
    if request.state == "open":
        _dispatch(
            engine,
            request,
            "satisfy",
            key=("insurance", request.id, "satisfy"),
            correlation_id=flow_correlation_id(claim.id),
        )
        request = _entity(
            persistence,
            "insurance_document_request",
            request.id,
        )
    if request.state == "satisfied":
        engine.scheduler.cancel_pending(
            entity_type="insurance_document_request",
            entity_id=request.id,
            name="expire",
        )
    claim = _claim(persistence, entities)
    if claim.state == "pending_documents":
        _dispatch(
            engine,
            claim,
            "documents_ready",
            key=("insurance", claim.id, "documents-ready", ordinal),
            correlation_id=flow_correlation_id(claim.id),
        )
    return True


def queue_claim(persistence, engine, backend, *, entities, ordinal=1):
    claim = _claim(persistence, entities)
    if claim.state not in {"ready_for_assessment", "reopened"}:
        raise RuntimeError(
            f"claim queue requires ready/reopened claim, got {claim.state}"
        )
    item_id = f"claim-queue:{claim.id}:{ordinal}"
    queued = any(i.item_id == item_id for i in persistence.store_items())
    consumed = any(
        result.item.item_id == item_id for result in persistence.store_get_results()
    )
    pending = any(i.item_id == item_id for i in persistence.store_put_intents())
    if not (queued or consumed or pending):
        engine.stores.put(
            backend,
            store_name="claim_queue",
            item_id=item_id,
            value={"claim_id": claim.id, "claim_type": claim.attributes["claim_type"]},
            priority=int(claim.attributes["severity"]),
            requested_at=backend.now,
        )
        backend.run_until(backend.now)


def claim_next_for_assessment(
    persistence,
    engine,
    backend,
    *,
    worker_id,
    assessment_ordinal=1,
):
    request_id = f"claims-adjuster:{worker_id}"
    if not engine.context.scenarios.attribute("insurance.adjuster.available", True):
        engine.resources.withdraw(backend, request_id)
        return None

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="claims_adjuster",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return None

    get_id = f"claim-pick:{worker_id}:{assessment_ordinal}"
    result = engine.stores.ensure_selection(
        backend,
        store_name="claim_queue",
        request_id=get_id,
        requested_at=backend.now,
    )
    if result is None:
        engine.resources.withdraw(backend, request_id)
        return None

    claim_id = str(result.item.value["claim_id"])
    claim = _entity(persistence, "insurance_claim", claim_id)
    if claim.state in {"ready_for_assessment", "reopened"}:
        _dispatch(
            engine,
            claim,
            "start_assessment",
            key=("insurance", claim.id, "start-assessment", assessment_ordinal),
            correlation_id=flow_correlation_id(claim.id),
        )
    assessment = persistence.entity(
        "insurance_assessment",
        assessment_id(claim.id, assessment_ordinal),
    )
    if assessment is None:
        assessment = engine.context.entities.create(
            Assessment,
            key=("insurance-reference", claim.id, "assessment", assessment_ordinal),
            state="pending",
            attributes={"claim_id": claim.id, "ordinal": assessment_ordinal},
        )
        with persistence.transaction() as uow:
            uow.save_entity(assessment)
    if assessment.state == "pending":
        _dispatch(
            engine,
            assessment,
            "start",
            key=("insurance", assessment.id, "start"),
            correlation_id=flow_correlation_id(claim.id),
        )
    return claim_id


def complete_assessment(
    persistence,
    engine,
    backend,
    *,
    entities,
    worker_id,
    outcome="approve",
    ordinal=1,
):
    if outcome not in {"approve", "reject", "fraud"}:
        raise ValueError(f"unsupported assessment outcome: {outcome}")
    claim = _claim(persistence, entities)
    assessment = _entity(
        persistence,
        "insurance_assessment",
        assessment_id(claim.id, ordinal),
    )
    request_id = f"claims-adjuster:{worker_id}"

    if assessment.state in {"approved", "rejected", "fraud_flagged"}:
        correlation_id = flow_correlation_id(claim.id)
        if claim.state == "assessing":
            claim_event = {
                "approved": "approve",
                "rejected": "reject",
                "fraud_flagged": "flag_fraud",
            }[assessment.state]
            _dispatch(
                engine,
                claim,
                claim_event,
                key=("insurance", claim.id, assessment.id, "reconcile-terminal"),
                correlation_id=correlation_id,
            )
        engine.resources.withdraw(backend, request_id)
        return assessment.state

    if claim.state != "assessing" or assessment.state != "in_progress":
        raise RuntimeError("assessment completion requires active claim assessment")

    correlation_id = flow_correlation_id(claim.id)
    event = {
        "approve": "approve",
        "reject": "reject",
        "fraud": "flag_fraud",
    }[outcome]
    _dispatch(
        engine,
        assessment,
        event,
        key=("insurance-assessment", assessment.id, event),
        correlation_id=correlation_id,
    )
    claim = _claim(persistence, entities)
    _dispatch(
        engine,
        claim,
        "flag_fraud" if outcome == "fraud" else outcome,
        key=("insurance", claim.id, event, ordinal),
        correlation_id=correlation_id,
    )
    engine.resources.withdraw(backend, request_id)
    return _entity(
        persistence,
        "insurance_assessment",
        assessment_id(claim.id, ordinal),
    ).state


def ensure_fraud_investigation(persistence, engine, *, entities):
    claim = _claim(persistence, entities)
    existing = persistence.entity(
        "insurance_fraud_investigation",
        fraud_investigation_id(claim.id),
    )
    if existing is not None:
        return existing
    if claim.state != "fraud_review":
        raise RuntimeError("fraud investigation requires Claim(fraud_review)")
    value = engine.context.entities.create(
        FraudInvestigation,
        key=("insurance-reference", claim.id, "fraud-investigation"),
        state="opened",
        attributes={"claim_id": claim.id},
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    return value


def reconcile_fraud(
    persistence,
    engine,
    backend,
    *,
    entities,
    confirm=False,
):
    claim = _claim(persistence, entities)
    investigation = ensure_fraud_investigation(
        persistence,
        engine,
        entities=entities,
    )
    request_id = f"fraud-investigator:{investigation.id}"
    if investigation.state in {"cleared", "confirmed"}:
        if claim.state == "fraud_review":
            event = "clear_fraud" if investigation.state == "cleared" else "reject"
            _dispatch(
                engine,
                claim,
                event,
                key=("insurance", claim.id, investigation.id, "reconcile-terminal"),
                correlation_id=flow_correlation_id(claim.id),
            )
        engine.resources.withdraw(backend, request_id)
        return investigation.state == "cleared"

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="fraud_investigator",
        request_id=request_id,
        requested_at=backend.now,
        priority=1,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(claim.id)
    if investigation.state == "opened":
        _dispatch(
            engine,
            investigation,
            "assign",
            key=("insurance-fraud", investigation.id, "assign"),
            correlation_id=correlation_id,
        )
        investigation = persistence.entity(
            "insurance_fraud_investigation", investigation.id
        )
    if investigation is not None and investigation.state == "assigned":
        _dispatch(
            engine,
            investigation,
            "confirm" if confirm else "clear",
            key=("insurance-fraud", investigation.id, "confirm" if confirm else "clear"),
            correlation_id=correlation_id,
        )

    claim = _claim(persistence, entities)
    if confirm and claim.state == "fraud_review":
        _dispatch(
            engine,
            claim,
            "reject",
            key=("insurance", claim.id, investigation.id, "fraud-reject"),
            correlation_id=correlation_id,
        )
    elif not confirm and claim.state == "fraud_review":
        _dispatch(
            engine,
            claim,
            "clear_fraud",
            key=("insurance", claim.id, investigation.id, "fraud-clear"),
            correlation_id=correlation_id,
        )
    engine.resources.withdraw(backend, request_id)
    return not confirm


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
    approved_assessment = next(
        (
            candidate
            for ordinal in (3, 2, 1)
            for candidate in (
                persistence.entity(
                    "insurance_assessment",
                    assessment_id(claim.id, ordinal),
                ),
            )
            if candidate is not None and candidate.state == "approved"
        ),
        None,
    )
    if claim.state != "approved" or approved_assessment is None:
        raise RuntimeError("reserve requires approved claim and assessment evidence")
    reserve_amount = float(amount if amount is not None else claim.attributes["amount"])
    reserve_units = _reference_minor_units(reserve_amount)
    if reserve_units <= 0:
        raise ValueError("reserve amount must be positive")
    reserve_amount = reserve_units / 100
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

    if payment.state not in {"due", "partially_paid"}:
        return False
    if reserve.state != "established":
        raise RuntimeError("payout requires established reserve")
    if not engine.context.scenarios.attribute("insurance.payment.available", True):
        engine.resources.withdraw(backend, request_id)
        return False

    partial_units = None
    if partial and payment.state == "due":
        partial_units = _reference_minor_units(float(payment.attributes["amount"]))
        if partial_units < 2:
            raise ValueError("partial payout requires at least two minor units")

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

    engine.resources.withdraw(backend, request_id)
    return reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
    )


def reopen_claim(persistence, engine, *, entities):
    claim = _claim(persistence, entities)
    if claim.state != "rejected":
        raise RuntimeError("only rejected claims can be reopened")
    _dispatch(
        engine,
        claim,
        "reopen",
        key=("insurance", claim.id, "reopen", claim.version),
        correlation_id=flow_correlation_id(claim.id),
    )
    return _claim(persistence, entities)
