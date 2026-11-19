"""Subscription plan-change request workflow (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import ChangeRequest, Subscription
from .foundation import (
    SubscriptionEntities,
    _change_entity,
    _subscription_entity,
    change_request_id,
    flow_correlation_id,
)


def _validate_plan_change_request(
    subscription: Subscription,
    *,
    target_plan: str,
    effective_at: datetime,
    now: datetime,
) -> None:
    if subscription.state != "active":
        raise RuntimeError("plan change requires active subscription")
    if target_plan == subscription.attributes["plan_code"]:
        raise ValueError("target plan must differ from active plan")
    term_end_at = datetime.fromisoformat(str(subscription.attributes["term_end_at"]))
    if effective_at <= now or effective_at >= term_end_at:
        raise ValueError("plan change must become effective inside the active term")


def _existing_change_if_compatible(
    persistence: MemoryPersistence,
    *,
    change_id: str,
    target_plan: str,
    effective_at: datetime,
) -> ChangeRequest | None:
    existing = persistence.entity("saas_change_request", change_id)
    if existing is None:
        return None
    expected = (target_plan, effective_at.isoformat())
    actual = (
        existing.attributes["to_plan"],
        existing.attributes["effective_at"],
    )
    if actual != expected:
        raise ValueError("change identity already exists with different intent")
    return existing


def _pending_plan_changes(
    persistence: MemoryPersistence,
    subscription: Subscription,
) -> list[ChangeRequest]:
    return [
        _change_entity(persistence, str(change_id))
        for change_id in subscription.attributes.get("change_request_ids", [])
        if persistence.entity("saas_change_request", str(change_id)) is not None
    ]


def _create_scheduled_change(
    engine: Engine,
    subscription: Subscription,
    *,
    ordinal: int,
    target_plan: str,
    effective_at: datetime,
) -> ChangeRequest:
    return engine.context.entities.create(
        ChangeRequest,
        key=("saas-reference", subscription.id, "change", ordinal),
        state="scheduled",
        attributes={
            "subscription_id": subscription.id,
            "ordinal": ordinal,
            "from_plan": subscription.attributes["plan_code"],
            "to_plan": target_plan,
            "from_entitlement_id": str(subscription.attributes["active_entitlement_id"]),
            "effective_at": effective_at.isoformat(),
        },
    )


def _schedule_plan_change(
    engine: Engine,
    *,
    change: ChangeRequest,
    subscription_id: str,
    effective_at: datetime,
) -> None:
    command = engine.context.commands.create(
        "apply",
        target=change,
        due_at=effective_at,
        correlation_id=flow_correlation_id(subscription_id),
        key=("saas-change", change.id, "apply"),
    )
    engine.context.schedules.at(effective_at, command=command)


def request_plan_change(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: SubscriptionEntities,
    ordinal: int,
    target_plan: str,
    effective_at: datetime,
) -> ChangeRequest:
    if ordinal <= 0:
        raise ValueError("change ordinal must be positive")
    subscription = _subscription_entity(persistence, entities)
    _validate_plan_change_request(
        subscription,
        target_plan=target_plan,
        effective_at=effective_at,
        now=backend.now,
    )

    cid = change_request_id(subscription.id, ordinal)
    existing = _existing_change_if_compatible(
        persistence,
        change_id=cid,
        target_plan=target_plan,
        effective_at=effective_at,
    )
    if existing is not None:
        return existing

    pending = _pending_plan_changes(persistence, subscription)
    if any(change.state == "scheduled" for change in pending):
        raise RuntimeError("subscription already has a pending plan change")

    change = _create_scheduled_change(
        engine,
        subscription,
        ordinal=ordinal,
        target_plan=target_plan,
        effective_at=effective_at,
    )
    change_ids = list(subscription.attributes.get("change_request_ids", []))
    change_ids.append(change.id)
    subscription.attributes["change_request_ids"] = change_ids
    with persistence.transaction() as uow:
        uow.save_entity(change)
        uow.save_entity(subscription)
    _schedule_plan_change(
        engine,
        change=change,
        subscription_id=subscription.id,
        effective_at=effective_at,
    )
    return change
