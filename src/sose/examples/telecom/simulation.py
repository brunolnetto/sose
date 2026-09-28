from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
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
    NetworkAlarm,
    ProductOrder,
    ServiceOrder,
    SubscriptionService,
    TroubleTicket,
    UsageRecord,
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
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=947),
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
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(persistence: MemoryPersistence) -> TelecomEntities:
    context, _ = build_runtime(persistence)
    order = context.entities.create(
        ProductOrder,
        key=("telecom-reference", "postpaid-mobile-order-1"),
        state="captured",
        attributes={
            "customer_id": "customer-1",
            "product_offering": "postpaid-mobile",
            "access_technology": "5G",
            "sim_type": "eSIM",
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(order)
        uow.save_resource_definition(
            ResourceDefinition("provisioning_worker", capacity=1)
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


def ensure_fulfillment_entities(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
) -> tuple[ServiceOrder, SubscriptionService]:
    order = _product_order(persistence, entities)
    correlation_id = flow_correlation_id(order.id)

    if order.state == "captured":
        _dispatch(
            engine,
            order,
            "acknowledge",
            key=("telecom-product-order", order.id, "acknowledge"),
            correlation_id=correlation_id,
        )
        order = _product_order(persistence, entities)
    if order.state == "acknowledged":
        _dispatch(
            engine,
            order,
            "start",
            key=("telecom-product-order", order.id, "start"),
            correlation_id=correlation_id,
        )
        order = _product_order(persistence, entities)
    if order.state not in {"in_progress", "completed"}:
        raise RuntimeError(f"order cannot be fulfilled from {order.state}")

    service_order = _service_order(persistence, order.id)
    service = _service(persistence, order.id)
    if service_order is None:
        service_order = engine.context.entities.create(
            ServiceOrder,
            key=("telecom-reference", order.id, "service-order"),
            state="pending",
            attributes={
                "product_order_id": order.id,
                "action": "add",
                "service_type": "mobile-connectivity",
            },
        )
    if service is None:
        service = engine.context.entities.create(
            SubscriptionService,
            key=("telecom-reference", order.id, "subscription-service"),
            state="designed",
            attributes={
                "product_order_id": order.id,
                "service_order_id": service_order.id,
                "access_technology": order.attributes["access_technology"],
                "sim_type": order.attributes["sim_type"],
                "subscriber_id": "subscriber-1",
                "open_incident_keys": [],
            },
        )

    with persistence.transaction() as uow:
        if persistence.entity("telecom_service_order", service_order.id) is None:
            uow.save_entity(service_order)
        if persistence.entity("telecom_subscription_service", service.id) is None:
            uow.save_entity(service)

    service_order = _entity(
        persistence,
        "telecom_service_order",
        service_order.id,
    )
    if service_order.state == "pending":
        _dispatch(
            engine,
            service_order,
            "accept",
            key=("telecom-service-order", service_order.id, "accept"),
            correlation_id=correlation_id,
        )
        service_order = _entity(
            persistence,
            "telecom_service_order",
            service_order.id,
        )
    return service_order, _entity(
        persistence,
        "telecom_subscription_service",
        service.id,
    )


def schedule_activation(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: TelecomEntities,
    delay: timedelta = ACTIVATION_DELAY,
) -> datetime:
    service_order, service = ensure_fulfillment_entities(
        persistence,
        engine,
        entities=entities,
    )
    correlation_id = flow_correlation_id(entities.product_order_id)

    if service.state == "active":
        return backend.now
    if service_order.state == "accepted":
        _dispatch(
            engine,
            service_order,
            "start_provisioning",
            key=("telecom-service-order", service_order.id, "start-provisioning"),
            correlation_id=correlation_id,
        )
        service_order = _entity(
            persistence,
            "telecom_service_order",
            service_order.id,
        )
    if service.state == "designed":
        _dispatch(
            engine,
            service,
            "start_provisioning",
            key=("telecom-service", service.id, "start-provisioning"),
            correlation_id=correlation_id,
        )
        service = _entity(
            persistence,
            "telecom_subscription_service",
            service.id,
        )

    if service.state == "activation_ready":
        return backend.now
    if service.state != "provisioning":
        raise RuntimeError(f"service cannot schedule activation from {service.state}")

    existing = engine.scheduler.find_pending(
        entity_type="telecom_subscription_service",
        entity_id=service.id,
        name="make_activation_ready",
    )
    if existing is not None:
        return existing.work.due_at

    due_at = backend.now + delay
    command = engine.context.commands.create(
        "make_activation_ready",
        target=service,
        due_at=due_at,
        correlation_id=correlation_id,
        key=("telecom-service", service.id, "activation-ready"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def reconcile_activation(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: TelecomEntities,
) -> bool:
    order = _product_order(persistence, entities)
    service_order = _service_order(persistence, order.id)
    service = _service(persistence, order.id)
    if service_order is None or service is None:
        return False

    request_id = f"provisioning-worker:{service.id}"
    correlation_id = flow_correlation_id(order.id)

    if service.state == "active":
        if service_order.state == "provisioning":
            _dispatch(
                engine,
                service_order,
                "complete",
                key=("telecom-service-order", service_order.id, "complete"),
                correlation_id=correlation_id,
            )
        order = _product_order(persistence, entities)
        if order.state == "in_progress":
            _dispatch(
                engine,
                order,
                "complete",
                key=("telecom-product-order", order.id, "complete"),
                correlation_id=correlation_id,
            )
        engine.resources.withdraw(backend, request_id)
        return True

    if service.state != "activation_ready":
        return False
    if not engine.context.scenarios.attribute(
        "telecom.provisioning.available",
        True,
    ):
        engine.resources.withdraw(backend, request_id)
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="provisioning_worker",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    service = _entity(
        persistence,
        "telecom_subscription_service",
        service.id,
    )
    if service.state == "activation_ready":
        _dispatch(
            engine,
            service,
            "activate",
            key=("telecom-service", service.id, "activate"),
            correlation_id=correlation_id,
        )
    service_order = _entity(
        persistence,
        "telecom_service_order",
        service_order.id,
    )
    if service_order.state == "provisioning":
        _dispatch(
            engine,
            service_order,
            "complete",
            key=("telecom-service-order", service_order.id, "complete"),
            correlation_id=correlation_id,
        )
    order = _product_order(persistence, entities)
    if order.state == "in_progress":
        _dispatch(
            engine,
            order,
            "complete",
            key=("telecom-product-order", order.id, "complete"),
            correlation_id=correlation_id,
        )
    engine.resources.withdraw(backend, request_id)
    return True


def record_usage(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
    sequence: int,
    quantity: float,
    unit: str = "MB",
) -> UsageRecord:
    if sequence <= 0:
        raise ValueError("sequence must be positive")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    service = _service(persistence, entities.product_order_id)
    if service is None:
        raise RuntimeError("usage requires active subscription service")

    uid = usage_record_id(service.id, sequence)
    existing = persistence.entity("telecom_usage_record", uid)
    if existing is not None:
        if (
            float(existing.attributes["quantity"]) != float(quantity)
            or existing.attributes["unit"] != unit
        ):
            raise ValueError("usage identity already exists with different measurement")
        if existing.state == "captured":
            _dispatch(
                engine,
                existing,
                "commit",
                key=("telecom-usage", existing.id, "commit"),
                correlation_id=flow_correlation_id(entities.product_order_id),
            )
            existing = _entity(
                persistence,
                "telecom_usage_record",
                existing.id,
            )
        return existing

    if service.state != "active":
        raise RuntimeError("usage requires active subscription service")

    usage = engine.context.entities.create(
        UsageRecord,
        key=("telecom-reference", service.id, "usage", sequence),
        state="captured",
        attributes={
            "service_id": service.id,
            "sequence": sequence,
            "quantity": float(quantity),
            "unit": unit,
            "rated": False,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(usage)
    _dispatch(
        engine,
        usage,
        "commit",
        key=("telecom-usage", usage.id, "commit"),
        correlation_id=flow_correlation_id(entities.product_order_id),
    )
    return _entity(persistence, "telecom_usage_record", usage.id)


def raise_service_alarm(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
    incident_key: str,
    severity: str = "major",
) -> tuple[NetworkAlarm, TroubleTicket]:
    if not incident_key:
        raise ValueError("incident_key must be non-empty")
    service = _service(persistence, entities.product_order_id)
    if service is None or service.state not in {"active", "suspended"}:
        raise RuntimeError("alarm requires provisioned subscription service")
    correlation_id = flow_correlation_id(entities.product_order_id)

    aid = alarm_id(service.id, incident_key)
    tid = trouble_ticket_id(service.id, incident_key)
    alarm = persistence.entity("telecom_network_alarm", aid)
    ticket = persistence.entity("telecom_trouble_ticket", tid)

    if alarm is None:
        alarm = engine.context.entities.create(
            NetworkAlarm,
            key=("telecom-reference", service.id, "alarm", incident_key),
            state="raised",
            attributes={
                "service_id": service.id,
                "incident_key": incident_key,
                "severity": severity,
            },
        )
    if ticket is None:
        ticket = engine.context.entities.create(
            TroubleTicket,
            key=("telecom-reference", service.id, "ticket", incident_key),
            state="open",
            attributes={
                "service_id": service.id,
                "alarm_id": alarm.id,
                "incident_key": incident_key,
                "severity": severity,
            },
        )
    with persistence.transaction() as uow:
        if persistence.entity("telecom_network_alarm", alarm.id) is None:
            uow.save_entity(alarm)
        if persistence.entity("telecom_trouble_ticket", ticket.id) is None:
            uow.save_entity(ticket)

    alarm = _entity(persistence, "telecom_network_alarm", alarm.id)
    ticket = _entity(persistence, "telecom_trouble_ticket", ticket.id)
    unresolved = alarm.state != "cleared" or ticket.state != "closed"

    service = _entity(
        persistence,
        "telecom_subscription_service",
        service.id,
    )
    open_incidents = list(service.attributes.get("open_incident_keys", []))
    if unresolved and incident_key not in open_incidents:
        open_incidents.append(incident_key)
        service.attributes["open_incident_keys"] = open_incidents
        with persistence.transaction() as uow:
            uow.save_entity(service)
    if unresolved and service.state == "active":
        _dispatch(
            engine,
            service,
            "suspend",
            key=("telecom-service", service.id, incident_key, "suspend"),
            correlation_id=correlation_id,
        )
    return (
        _entity(persistence, "telecom_network_alarm", alarm.id),
        _entity(persistence, "telecom_trouble_ticket", ticket.id),
    )


def acknowledge_incident(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
    incident_key: str,
) -> None:
    service = _service(persistence, entities.product_order_id)
    if service is None:
        raise RuntimeError("service was not provisioned")
    correlation_id = flow_correlation_id(entities.product_order_id)
    alarm = _entity(
        persistence,
        "telecom_network_alarm",
        alarm_id(service.id, incident_key),
    )
    ticket = _entity(
        persistence,
        "telecom_trouble_ticket",
        trouble_ticket_id(service.id, incident_key),
    )
    if alarm.state == "raised":
        _dispatch(
            engine,
            alarm,
            "acknowledge",
            key=("telecom-alarm", alarm.id, "acknowledge"),
            correlation_id=correlation_id,
        )
    if ticket.state == "open":
        _dispatch(
            engine,
            ticket,
            "acknowledge",
            key=("telecom-ticket", ticket.id, "acknowledge"),
            correlation_id=correlation_id,
        )


def restore_service(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
    incident_key: str,
) -> bool:
    service = _service(persistence, entities.product_order_id)
    if service is None:
        return False
    correlation_id = flow_correlation_id(entities.product_order_id)
    alarm = _entity(
        persistence,
        "telecom_network_alarm",
        alarm_id(service.id, incident_key),
    )
    ticket = _entity(
        persistence,
        "telecom_trouble_ticket",
        trouble_ticket_id(service.id, incident_key),
    )

    if alarm.state in {"raised", "acknowledged"}:
        _dispatch(
            engine,
            alarm,
            "clear",
            key=("telecom-alarm", alarm.id, "clear"),
            correlation_id=correlation_id,
        )
    ticket = _entity(persistence, "telecom_trouble_ticket", ticket.id)
    if ticket.state in {"open", "acknowledged"}:
        _dispatch(
            engine,
            ticket,
            "resolve",
            key=("telecom-ticket", ticket.id, "resolve"),
            correlation_id=correlation_id,
        )
        ticket = _entity(persistence, "telecom_trouble_ticket", ticket.id)
    if ticket.state == "resolved":
        _dispatch(
            engine,
            ticket,
            "close",
            key=("telecom-ticket", ticket.id, "close"),
            correlation_id=correlation_id,
        )
    alarm = _entity(persistence, "telecom_network_alarm", alarm.id)
    ticket = _entity(persistence, "telecom_trouble_ticket", ticket.id)
    service = _entity(
        persistence,
        "telecom_subscription_service",
        service.id,
    )
    if alarm.state == "cleared" and ticket.state == "closed":
        open_incidents = [
            key
            for key in service.attributes.get("open_incident_keys", [])
            if key != incident_key
        ]
        if open_incidents != service.attributes.get("open_incident_keys", []):
            service.attributes["open_incident_keys"] = open_incidents
            with persistence.transaction() as uow:
                uow.save_entity(service)
            service = _entity(
                persistence,
                "telecom_subscription_service",
                service.id,
            )

    if (
        service.state == "suspended"
        and not service.attributes.get("open_incident_keys", [])
    ):
        _dispatch(
            engine,
            service,
            "restore",
            key=("telecom-service", service.id, incident_key, "restore"),
            correlation_id=correlation_id,
        )
    return True


def run_happy_path() -> tuple[MemoryPersistence, TelecomEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    due_at = schedule_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(due_at)
    if not reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("telecom activation failed")
    record_usage(
        persistence,
        engine,
        entities=entities,
        sequence=1,
        quantity=512.0,
    )
    return persistence, entities
