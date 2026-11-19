"""Subscription constants, ids, runtime/seed setup and lookups (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import ChangeRequest, Entitlement, Subscription
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
    owner_id: str,
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
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    customer_id: str = "customer-1",
    initial_plan: str = "basic",
    term_duration: timedelta = TERM_DURATION,
) -> SubscriptionEntities:
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
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
