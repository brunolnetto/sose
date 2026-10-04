from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import InsuranceConfig
from .simulation import (
    assessment_id,
    build_runtime,
    claim_next_for_assessment,
    complete_assessment,
    document_request_id,
    ensure_document_request,
    ensure_reserve,
    payment_id,
    queue_claim,
    reconcile_fraud,
    reconcile_payment,
    schedule_payment,
    seed_reference,
    satisfy_documents,
)


def _build(
    persistence: Persistence,
    config: InsuranceConfig,
    now: datetime,
    tick: int,
):
    return build_runtime(
        persistence,
        now=now,
        tick=tick,
        step=config.tick_step,
        random_seed=config.random_seed,
    )


def _seed(persistence: Persistence, config: InsuranceConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        severity=config.severity,
        amount=config.amount,
        claim_type=config.claim_type,
        currency=config.currency,
    )

def _claim_or_error(persistence, entities):
    claim = persistence.entity("insurance_claim", entities.claim_id)
    if claim is None:
        raise RuntimeError("insurance claim was not persisted")
    return claim


def _maybe_satisfy_documents(
    persistence,
    engine,
    config,
    *,
    entities,
    claim,
) -> bool:
    if claim.state != "pending_documents":
        return False
    request = persistence.entity(
        "insurance_document_request",
        document_request_id(claim.id, 1),
    )
    if (
        config.auto_satisfy_documents
        and request is not None
        and request.state == "open"
    ):
        satisfy_documents(
            persistence,
            engine,
            entities=entities,
        )
    return True


def _maybe_assess_claim(
    persistence,
    engine,
    backend,
    config,
    *,
    entities,
    claim,
) -> bool:
    if claim.state != "assessing":
        return False
    assessment = persistence.entity(
        "insurance_assessment",
        assessment_id(claim.id, 1),
    )
    if assessment is not None and assessment.state == "in_progress":
        complete_assessment(
            persistence,
            engine,
            backend,
            entities=entities,
            worker_id="recurring-job",
            outcome=config.assessment_outcome,
        )
    return True


def _maybe_reconcile_payment(
    persistence,
    engine,
    backend,
    config,
    *,
    entities,
    claim,
) -> None:
    if claim.state != "payment_scheduled":
        return
    payment = persistence.entity(
        "insurance_payment",
        payment_id(claim.id),
    )
    if payment is None or payment.state not in {"due", "partially_paid", "paid"}:
        return
    reconcile_payment(
        persistence,
        engine,
        backend,
        entities=entities,
        partial=config.partial_payment,
    )


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    claim = _claim_or_error(persistence, entities)

    if claim.state in {"opened", "reopened"}:
        ensure_document_request(
            persistence,
            engine,
            backend,
            entities=entities,
            deadline=config.document_deadline,
        )
        return

    if _maybe_satisfy_documents(
        persistence,
        engine,
        config,
        entities=entities,
        claim=claim,
    ):
        return

    if claim.state == "ready_for_assessment":
        queue_claim(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        claim_next_for_assessment(
            persistence,
            engine,
            backend,
            worker_id="recurring-job",
        )
        return

    if _maybe_assess_claim(
        persistence,
        engine,
        backend,
        config,
        entities=entities,
        claim=claim,
    ):
        return

    if claim.state == "fraud_review":
        reconcile_fraud(
            persistence,
            engine,
            backend,
            entities=entities,
            confirm=config.fraud_confirmed,
        )
        return

    if claim.state == "approved":
        ensure_reserve(
            persistence,
            engine,
            entities=entities,
        )
        schedule_payment(
            persistence,
            engine,
            backend,
            entities=entities,
            delay=config.payment_delay,
        )
        return

    _maybe_reconcile_payment(
        persistence,
        engine,
        backend,
        config,
        entities=entities,
        claim=claim,
    )


definition = DomainDefinition(
    name="insurance",
    description="Insurance claims reference domain.",
    config_model=InsuranceConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_satisfy_documents","assessment_outcome","fraud_confirmed","partial_payment"]),
)
