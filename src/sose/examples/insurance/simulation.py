from __future__ import annotations


from sose.backends.simpy import SimPyBackend  # noqa: F401
from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine  # noqa: F401
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import ResourceDefinition, StoreDefinition  # noqa: F401
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence  # noqa: F401

from .entities import (  # noqa: F401
    Assessment,
    Claim,
    DocumentRequest,
    FraudInvestigation,
    Payment,
    Policy,
    Reserve,
)
from .scenarios import ORIGIN  # noqa: F401
from .statecharts import (  # noqa: F401
    AssessmentChart,
    ClaimChart,
    DocumentRequestChart,
    FraudInvestigationChart,
    PaymentChart,
    PolicyChart,
    ReserveChart,
)
from .foundation import (  # noqa: F401
    DOCUMENT_DEADLINE,
    InsuranceEntities,
    _claim,
    _dispatch,
    _entity,
    _policy,
    assessment_id,
    build_runtime,
    document_request_id,
    flow_correlation_id,
    payment_id,
    seed_reference,
)
from .fraud import (  # noqa: F401
    reconcile_fraud,
)
from .payments import (  # noqa: F401
    ensure_reserve,
    reconcile_payment,
    schedule_payment,
)


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


# Moved by the quality-ratchet splits (#632); importing these names from here
# is deprecated (ADR-0011). Import them from the module named on the right.
from sose._deprecation import deprecated_reexports as _deprecated_reexports

__getattr__ = _deprecated_reexports(__name__, {
    "deterministic_id": "sose.core.identity",
    "ensure_fraud_investigation": "sose.examples.insurance.fraud",
    "ensure_payment": "sose.examples.insurance.payments",
    "fraud_investigation_id": "sose.examples.insurance.foundation",
    "reserve_id": "sose.examples.insurance.foundation",
}, since="0.9.0", removed_in="0.10.0")
