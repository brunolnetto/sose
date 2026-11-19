"""Record-to-report reconciliation adjustment workflow (split from simulation.py)."""

from __future__ import annotations


from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .identity import flow_correlation_id
from .entities import (
    Adjustment,
)
from .foundation import (
    R2REntities,
    _adjustment,
    _reconciliation,
)


def _dispatch(*args, **kwargs):
    """Forward to ``simulation._dispatch`` at call time so patches on it still apply."""
    from . import simulation

    return simulation._dispatch(*args, **kwargs)


def ensure_adjustment(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: R2REntities,
) -> Adjustment:
    existing = _adjustment(persistence, entities)
    if existing is not None:
        return existing

    item = _reconciliation(persistence, entities)
    if item.state == "unmatched":
        _dispatch(
            engine,
            item,
            "require_adjustment",
            key=("r2r", item.id, "require-adjustment"),
            correlation_id=flow_correlation_id(entities.period_id),
        )
        item = _reconciliation(persistence, entities)

    if item.state != "adjustment_required":
        raise RuntimeError(
            "adjustment requires ReconciliationItem(adjustment_required)"
        )

    adjustment = engine.context.entities.create(
        Adjustment,
        key=("r2r-reference", item.id, "adjustment-1"),
        state="proposed",
        attributes={
            "period_id": entities.period_id,
            "reconciliation_id": item.id,
            "amount": float(item.attributes["amount"]),
            "currency": item.attributes["currency"],
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(adjustment)
    return adjustment


def post_adjustment(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: R2REntities,
    reject: bool = False,
) -> bool:
    adjustment = ensure_adjustment(
        persistence,
        engine,
        entities=entities,
    )
    request_id = f"posting-adjustment:{adjustment.id}"
    if adjustment.state in {"posted", "rejected"}:
        engine.resources.withdraw(backend, request_id)
        return _apply_posted_adjustment_if_needed(
            persistence,
            engine,
            entities=entities,
            adjustment=adjustment,
        )

    available = bool(
        engine.context.scenarios.attribute("r2r.posting.available", True)
    )
    if not available:
        engine.resources.withdraw(backend, request_id)
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="posting_processor",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(entities.period_id)
    adjustment = _adjustment_or_error(persistence, entities)
    adjustment = _progress_adjustment_submission(
        persistence,
        engine,
        entities=entities,
        adjustment=adjustment,
        correlation_id=correlation_id,
    )
    _post_or_reject_adjustment_if_approved(
        engine,
        adjustment=adjustment,
        correlation_id=correlation_id,
        reject=reject,
    )

    engine.resources.withdraw(backend, request_id)

    adjustment = _adjustment(persistence, entities)
    if adjustment is None or adjustment.state != "posted":
        return False
    return _apply_posted_adjustment_if_needed(
        persistence,
        engine,
        entities=entities,
        adjustment=adjustment,
    )


def _apply_posted_adjustment_if_needed(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: R2REntities,
    adjustment: Adjustment,
) -> bool:
    if adjustment.state != "posted":
        return False
    item = _reconciliation(persistence, entities)
    if item.state == "adjustment_required":
        _dispatch(
            engine,
            item,
            "apply_adjustment",
            key=("r2r", item.id, adjustment.id, "apply-adjustment"),
            correlation_id=flow_correlation_id(entities.period_id),
        )
    return True


def _adjustment_or_error(
    persistence: MemoryPersistence,
    entities: R2REntities,
) -> Adjustment:
    adjustment = _adjustment(persistence, entities)
    if adjustment is None:
        raise RuntimeError("adjustment disappeared")
    return adjustment


def _progress_adjustment_submission(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: R2REntities,
    adjustment: Adjustment,
    correlation_id: str,
) -> Adjustment:
    if adjustment.state == "proposed":
        _dispatch(
            engine,
            adjustment,
            "submit",
            key=("r2r-adjustment", adjustment.id, "submit"),
            correlation_id=correlation_id,
        )
        adjustment = _adjustment_or_error(persistence, entities)

    if adjustment.state == "submitted":
        _dispatch(
            engine,
            adjustment,
            "approve",
            key=("r2r-adjustment", adjustment.id, "approve"),
            correlation_id=correlation_id,
        )
        adjustment = _adjustment_or_error(persistence, entities)
    return adjustment


def _post_or_reject_adjustment_if_approved(
    engine: Engine,
    *,
    adjustment: Adjustment,
    correlation_id: str,
    reject: bool,
) -> None:
    if adjustment.state != "approved":
        return
    event = "reject" if reject else "post"
    _dispatch(
        engine,
        adjustment,
        event,
        key=("r2r-adjustment", adjustment.id, event),
        correlation_id=correlation_id,
    )
