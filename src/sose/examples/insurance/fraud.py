"""Insurance fraud-investigation workflow (split from simulation.py)."""

from __future__ import annotations

from .entities import FraudInvestigation
from .foundation import _claim, _dispatch, flow_correlation_id, fraud_investigation_id


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
    terminal = _reconcile_fraud_terminal_if_any(
        persistence,
        engine,
        backend,
        claim=claim,
        investigation=investigation,
        request_id=request_id,
    )
    if terminal is not None:
        return terminal

    if engine.resources.ensure_requested(
        backend,
        resource_name="fraud_investigator",
        request_id=request_id,
        requested_at=backend.now,
        priority=1,
    ) is None:
        return False

    correlation_id = flow_correlation_id(claim.id)
    investigation = _progress_fraud_investigation(
        persistence,
        engine,
        investigation=investigation,
        confirm=confirm,
        correlation_id=correlation_id,
    )
    claim = _claim(persistence, entities)
    _dispatch_claim_fraud_outcome(
        engine,
        claim=claim,
        investigation=investigation,
        confirm=confirm,
        correlation_id=correlation_id,
    )
    engine.resources.withdraw(backend, request_id)
    return not confirm


def _reconcile_fraud_terminal_if_any(
    persistence,
    engine,
    backend,
    *,
    claim,
    investigation,
    request_id: str,
) -> bool | None:
    if investigation.state not in {"cleared", "confirmed"}:
        return None
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


def _progress_fraud_investigation(
    persistence,
    engine,
    *,
    investigation,
    confirm: bool,
    correlation_id: str,
):
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
        event = "confirm" if confirm else "clear"
        _dispatch(
            engine,
            investigation,
            event,
            key=("insurance-fraud", investigation.id, event),
            correlation_id=correlation_id,
        )
    return investigation


def _dispatch_claim_fraud_outcome(
    engine,
    *,
    claim,
    investigation,
    confirm: bool,
    correlation_id: str,
) -> None:
    if claim.state != "fraud_review":
        return
    event = "reject" if confirm else "clear_fraud"
    key_suffix = "fraud-reject" if confirm else "fraud-clear"
    _dispatch(
        engine,
        claim,
        event,
        key=("insurance", claim.id, investigation.id, key_suffix),
        correlation_id=correlation_id,
    )
