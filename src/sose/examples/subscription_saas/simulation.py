from __future__ import annotations

from dataclasses import dataclass  # noqa: F401
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine
from sose.core.identity import deterministic_id  # noqa: F401
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import ChangeRequest, Entitlement, Subscription, SubscriptionOccurrence  # noqa: F401
from .scenarios import ORIGIN
from .statecharts import (  # noqa: F401
    ChangeRequestChart,
    EntitlementChart,
    SubscriptionChart,
    SubscriptionOccurrenceChart,
)
from .foundation import (  # noqa: F401
    SubscriptionEntities,
    TERM_DURATION,
    _change_entity,
    _dispatch,
    _entitlement_entity,
    _entity,
    _subscription_entity,
    build_runtime,
    change_request_id,
    entitlement_id,
    flow_correlation_id,
    occurrence_id,
    seed_reference,
)
from .plan_changes import (  # noqa: F401
    _create_scheduled_change,
    _existing_change_if_compatible,
    _pending_plan_changes,
    _schedule_plan_change,
    _validate_plan_change_request,
    request_plan_change,
)


def reconcile_plan_change(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: SubscriptionEntities,
    change_id: str,
) -> Entitlement | None:
    subscription = _subscription_entity(persistence, entities)
    change = _change_entity(persistence, change_id)
    if change.state == "scheduled":
        return None
    if change.state != "applied":
        raise RuntimeError("only an applied change can be reconciled")

    old_entitlement = _entitlement_entity(
        persistence,
        str(change.attributes["from_entitlement_id"]),
    )
    new_id = entitlement_id(subscription.id, f"change-{change.attributes['ordinal']}")
    new_entitlement = persistence.entity("saas_entitlement", new_id)
    if new_entitlement is None:
        new_entitlement = engine.context.entities.create(
            Entitlement,
            key=(
                "saas-reference",
                subscription.id,
                f"change-{change.attributes['ordinal']}",
            ),
            state="active",
            attributes={
                "subscription_id": subscription.id,
                "plan_code": change.attributes["to_plan"],
                "effective_at": change.attributes["effective_at"],
                "supersedes_entitlement_id": old_entitlement.id,
                "change_request_id": change.id,
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(new_entitlement)

    old_entitlement = _entitlement_entity(persistence, old_entitlement.id)
    if old_entitlement.state == "active":
        _dispatch(
            engine,
            old_entitlement,
            "supersede",
            key=("saas-entitlement", old_entitlement.id, change.id, "supersede"),
        )

    subscription = _subscription_entity(persistence, entities)
    entitlement_ids = list(subscription.attributes.get("entitlement_ids", []))
    if new_entitlement.id not in entitlement_ids:
        entitlement_ids.append(new_entitlement.id)
    subscription.attributes["entitlement_ids"] = entitlement_ids
    subscription.attributes["active_entitlement_id"] = new_entitlement.id
    subscription.attributes["plan_code"] = change.attributes["to_plan"]

    oid = occurrence_id(
        subscription.id,
        "plan-change",
        int(change.attributes["ordinal"]),
    )
    occurrence = persistence.entity("saas_subscription_occurrence", oid)
    if occurrence is None:
        occurrence = engine.context.entities.create(
            SubscriptionOccurrence,
            key=(
                "saas-reference",
                subscription.id,
                "plan-change",
                int(change.attributes["ordinal"]),
            ),
            state="captured",
            attributes={
                "subscription_id": subscription.id,
                "kind": "plan_change",
                "change_request_id": change.id,
                "from_entitlement_id": old_entitlement.id,
                "to_entitlement_id": new_entitlement.id,
                "from_plan": change.attributes["from_plan"],
                "to_plan": change.attributes["to_plan"],
                "effective_at": change.attributes["effective_at"],
            },
        )
        occurrence_ids = list(subscription.attributes.get("occurrence_ids", []))
        occurrence_ids.append(occurrence.id)
        subscription.attributes["occurrence_ids"] = occurrence_ids
        with persistence.transaction() as uow:
            uow.save_entity(subscription)
            uow.save_entity(occurrence)
    else:
        with persistence.transaction() as uow:
            uow.save_entity(subscription)

    occurrence = _entity(
        persistence,
        "saas_subscription_occurrence",
        oid,
    )
    if occurrence.state == "captured":
        _dispatch(
            engine,
            occurrence,
            "commit",
            key=("saas-occurrence", occurrence.id, "commit"),
        )
    return _entitlement_entity(persistence, new_entitlement.id)


def request_cancellation(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: SubscriptionEntities,
) -> datetime:
    subscription = _subscription_entity(persistence, entities)
    if subscription.state == "ended":
        return datetime.fromisoformat(str(subscription.attributes["term_end_at"]))
    if subscription.state == "active":
        # Cancellation-at-period-end freezes future commercial amendments.
        # Any still-pending plan change loses durable scheduler ownership before
        # cancellation becomes authoritative.
        for change_id in subscription.attributes.get("change_request_ids", []):
            change = persistence.entity("saas_change_request", str(change_id))
            if change is None or change.state != "scheduled":
                continue
            engine.scheduler.cancel_pending(
                entity_type="saas_change_request",
                entity_id=change.id,
                name="apply",
            )
            _dispatch(
                engine,
                change,
                "cancel",
                key=("saas-change", change.id, "cancel-for-subscription-end"),
            )
        _dispatch(
            engine,
            subscription,
            "request_cancel",
            key=("saas-subscription", subscription.id, "request-cancel"),
        )
        subscription = _subscription_entity(persistence, entities)
    if subscription.state != "cancellation_pending":
        raise RuntimeError("cancellation requires active subscription")

    term_end_at = datetime.fromisoformat(str(subscription.attributes["term_end_at"]))
    entitlement = _entitlement_entity(
        persistence,
        str(subscription.attributes["active_entitlement_id"]),
    )
    if engine.scheduler.find_pending(
        entity_type="saas_subscription",
        entity_id=subscription.id,
        name="end",
    ) is None:
        command = engine.context.commands.create(
            "end",
            target=subscription,
            due_at=term_end_at,
            correlation_id=flow_correlation_id(subscription.id),
            key=("saas-subscription", subscription.id, "end"),
        )
        engine.context.schedules.at(term_end_at, command=command)
    if entitlement.state == "active" and engine.scheduler.find_pending(
        entity_type="saas_entitlement",
        entity_id=entitlement.id,
        name="revoke",
    ) is None:
        command = engine.context.commands.create(
            "revoke",
            target=entitlement,
            due_at=term_end_at,
            correlation_id=flow_correlation_id(subscription.id),
            key=("saas-entitlement", entitlement.id, "revoke-at-end"),
        )
        engine.context.schedules.at(term_end_at, command=command)
    return term_end_at


def withdraw_cancellation(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: SubscriptionEntities,
) -> Subscription:
    subscription = _subscription_entity(persistence, entities)
    if subscription.state != "cancellation_pending":
        raise RuntimeError("withdrawal requires pending cancellation")
    entitlement = _entitlement_entity(
        persistence,
        str(subscription.attributes["active_entitlement_id"]),
    )
    engine.scheduler.cancel_pending(
        entity_type="saas_subscription",
        entity_id=subscription.id,
        name="end",
    )
    engine.scheduler.cancel_pending(
        entity_type="saas_entitlement",
        entity_id=entitlement.id,
        name="revoke",
    )
    _dispatch(
        engine,
        subscription,
        "withdraw_cancel",
        key=("saas-subscription", subscription.id, "withdraw-cancel"),
    )
    return _subscription_entity(persistence, entities)


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, SubscriptionEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    change = request_plan_change(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        target_plan="pro",
        effective_at=ORIGIN + timedelta(days=5),
    )
    backend.run_until(ORIGIN + timedelta(days=5))
    entitlement = reconcile_plan_change(
        persistence,
        engine,
        entities=entities,
        change_id=change.id,
    )
    if entitlement is None:
        raise RuntimeError("scheduled plan change did not become applicable")
    return persistence, entities
