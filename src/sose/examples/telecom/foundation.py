"""Telecom constants, ids, runtime/seed setup and lookups (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import (
    ProductOrder,
    ServiceOrder,
    SubscriptionService,
)
from .scenarios import ORIGIN
from .statecharts import (
    NetworkAlarmChart,
    ProductOrderChart,
    ServiceOrderChart,
    SubscriptionServiceChart,
    TroubleTicketChart,
    UsageRecordChart,
)


ACTIVATION_DELAY = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class TelecomEntities:
    product_order_id: str


def flow_correlation_id(product_order_id: str) -> str:
    return deterministic_id("telecom-order-flow", product_order_id)


def service_order_id(product_order_id: str) -> str:
    return deterministic_id(
        "entity",
        "telecom_service_order",
        "telecom-reference",
        product_order_id,
        "service-order",
    )


def subscription_service_id(product_order_id: str) -> str:
    return deterministic_id(
        "entity",
        "telecom_subscription_service",
        "telecom-reference",
        product_order_id,
        "subscription-service",
    )


def usage_record_id(service_id: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "telecom_usage_record",
        "telecom-reference",
        service_id,
        "usage",
        sequence,
    )


def alarm_id(service_id: str, incident_key: str) -> str:
    return deterministic_id(
        "entity",
        "telecom_network_alarm",
        "telecom-reference",
        service_id,
        "alarm",
        incident_key,
    )


def trouble_ticket_id(service_id: str, incident_key: str) -> str:
    return deterministic_id(
        "entity",
        "telecom_trouble_ticket",
        "telecom-reference",
        service_id,
        "ticket",
        incident_key,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 947,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("telecom_product_order", ProductOrderChart))
    registry.register(EntityType("telecom_service_order", ServiceOrderChart))
    registry.register(
        EntityType("telecom_subscription_service", SubscriptionServiceChart)
    )
    registry.register(EntityType("telecom_usage_record", UsageRecordChart))
    registry.register(EntityType("telecom_network_alarm", NetworkAlarmChart))
    registry.register(EntityType("telecom_trouble_ticket", TroubleTicketChart))
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
    product_offering: str = "postpaid-mobile",
    access_technology: str = "5G",
    sim_type: str = "eSIM",
    provisioning_capacity: int = 1,
) -> TelecomEntities:
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
    order = context.entities.create(
        ProductOrder,
        key=("telecom-reference", "postpaid-mobile-order-1"),
        state="captured",
        attributes={
            "customer_id": customer_id,
            "product_offering": product_offering,
            "access_technology": access_technology,
            "sim_type": sim_type,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(order)
        uow.save_resource_definition(
            ResourceDefinition("provisioning_worker", capacity=provisioning_capacity)
        )
    return TelecomEntities(product_order_id=order.id)


def _entity(persistence, entity_type: str, entity_id: str):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _product_order(persistence, entities: TelecomEntities) -> ProductOrder:
    return _entity(
        persistence,
        "telecom_product_order",
        entities.product_order_id,
    )


def _service_order(persistence, product_order_id_value: str) -> ServiceOrder | None:
    return persistence.entity(
        "telecom_service_order",
        service_order_id(product_order_id_value),
    )


def _service(persistence, product_order_id_value: str) -> SubscriptionService | None:
    return persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(product_order_id_value),
    )


def _dispatch(engine, entity, event: str, *, key, correlation_id: str) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)
