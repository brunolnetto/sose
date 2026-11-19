from __future__ import annotations

from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import ResourceDefinition  # noqa: F401
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import (  # noqa: F401
    NetworkAlarm,
    ProductOrder,
    ServiceOrder,
    SubscriptionService,
    TroubleTicket,
    UsageRecord,
)
from .scenarios import ORIGIN
from .statecharts import (  # noqa: F401
    NetworkAlarmChart,
    ProductOrderChart,
    ServiceOrderChart,
    SubscriptionServiceChart,
    TroubleTicketChart,
    UsageRecordChart,
)
from .foundation import (  # noqa: F401
    ACTIVATION_DELAY,
    TelecomEntities,
    _dispatch,
    _entity,
    _product_order,
    _service,
    _service_order,
    build_runtime,
    flow_correlation_id,
    seed_reference,
)
from .operations import (  # noqa: F401
    record_usage,
)


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
        _complete_activation_if_active(
            persistence,
            engine,
            entities=entities,
            service_order=service_order,
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

    if engine.resources.ensure_requested(
        backend,
        resource_name="provisioning_worker",
        request_id=request_id,
        requested_at=backend.now,
    ) is None:
        return False

    _activate_service_if_ready(
        persistence,
        engine,
        service_id=service.id,
        correlation_id=correlation_id,
    )
    _complete_activation_if_active(
        persistence,
        engine,
        entities=entities,
        service_order=service_order,
        correlation_id=correlation_id,
    )
    engine.resources.withdraw(backend, request_id)
    return True


def _complete_activation_if_active(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
    service_order,
    correlation_id: str,
) -> None:
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


def _activate_service_if_ready(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    service_id: str,
    correlation_id: str,
) -> None:
    service = _entity(
        persistence,
        "telecom_subscription_service",
        service_id,
    )
    if service.state != "activation_ready":
        return
    _dispatch(
        engine,
        service,
        "activate",
        key=("telecom-service", service.id, "activate"),
        correlation_id=correlation_id,
    )


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, TelecomEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
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
