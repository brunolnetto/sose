from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import ChangeRequest, Entitlement, Subscription, SubscriptionOccurrence
from .scenarios import ORIGIN
from .statecharts import (
    ChangeRequestChart,
    EntitlementChart,
    SubscriptionChart,
    SubscriptionOccurrenceChart,
)


TERM_DURATION = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class SubscriptionEntities:
    subscription_id: str


def flow_correlation_id(subscription_id: str) -> str:
    return deterministic_id("saas-subscription-flow", subscription_id)


def entitlement_id(subscription_id: str, key: str) -> str:
    return deterministic_id(
        "entity",
        "saas_entitlement",
        "saas-reference",
        subscription_id,
        key,
    )


def change_request_id(subscription_id: str, ordinal: int) -> str:
    return deterministic_id(
        "entity",
        "saas_change_request",
        "saas-reference",
        subscription_id,
        "change",
        ordinal,
    )


def occurrence_id(subscription_id: str, kind: str, ordinal: int) -> str:
    return deterministic_id(
        "entity",
        "saas_subscription_occurrence",
        "saas-reference",
        subscription_id,
        kind,
        ordinal,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 1531,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("saas_subscription", SubscriptionChart))
    registry.register(EntityType("saas_entitlement", EntitlementChart))
    registry.register(EntityType("saas_change_request", ChangeRequestChart))
    registry.register(
        EntityType("saas_subscription_occurrence", SubscriptionOccurrenceChart)
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
    now: datetime = ORIGIN,
    customer_id: str = "customer-1",
    initial_plan: str = "basic",
    term_duration: timedelta = TERM_DURATION,
) -> SubscriptionEntities:
    context, _ = build_runtime(persistence, now=now)
    subscription = context.entities.create(
        Subscription,
        key=("saas-reference", "subscription-1"),
        state="active",
        attributes={
            "customer_id": customer_id,
            "plan_code": initial_plan,
            "term_start_at": now.isoformat(),
            "term_end_at": (now + term_duration).isoformat(),
            "active_entitlement_id": None,
            "entitlement_ids": [],
            "change_request_ids": [],
            "occurrence_ids": [],
        },
    )
    entitlement = context.entities.create(
        Entitlement,
        key=("saas-reference", subscription.id, f"entitlement-{initial_plan}"),
        state="active",
        attributes={
            "subscription_id": subscription.id,
            "plan_code": initial_plan,
            "effective_at": now.isoformat(),
        },
    )
    subscription.attributes["active_entitlement_id"] = entitlement.id
    subscription.attributes["entitlement_ids"] = [entitlement.id]
    with persistence.transaction() as uow:
        uow.save_entity(subscription)
        uow.save_entity(entitlement)
    return SubscriptionEntities(subscription_id=subscription.id)


def _entity(persistence, entity_type: str, entity_id: str):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _subscription_entity(
    persistence: MemoryPersistence,
    entities: SubscriptionEntities,
) -> Subscription:
    return _entity(persistence, "saas_subscription", entities.subscription_id)


def _entitlement_entity(
    persistence: MemoryPersistence,
    entitlement_id_value: str,
) -> Entitlement:
    return _entity(persistence, "saas_entitlement", entitlement_id_value)


def _change_entity(
    persistence: MemoryPersistence,
    change_id: str,
) -> ChangeRequest:
    return _entity(persistence, "saas_change_request", change_id)


def _dispatch(engine: Engine, entity, event: str, *, key: tuple[object, ...]) -> None:
    subscription_id = str(
        entity.attributes.get("subscription_id")
        or entity.id
    )
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=flow_correlation_id(subscription_id),
        key=key,
    )
    engine.dispatch(command)


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


def run_happy_path() -> tuple[MemoryPersistence, SubscriptionEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
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
