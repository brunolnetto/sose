from __future__ import annotations

from dataclasses import dataclass  # noqa: F401 - historical import location
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock  # noqa: F401 - historical import location
from sose.core.context import SimulationContext  # noqa: F401 - historical import location
from sose.core.engine import Engine
from sose.core.identity import deterministic_id  # noqa: F401 - historical import location
from sose.core.randomness import RandomSource  # noqa: F401 - historical import location
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler  # noqa: F401 - historical import location
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import CollectionCase, Receivable, SalesOrder
from .scenarios import ORIGIN
from .statecharts import (  # noqa: F401 - historical import location
    CollectionCaseChart,
    ReceivableChart,
    SalesOrderChart,
)
from .records import (  # noqa: F401 - historical import location
    COLLECTION_FOLLOWUP_DELAY,
    DUE_DELAY,
    OVERDUE_DELAY,
    O2CEntities,
    build_runtime,
    collection_case_id,
    flow_correlation_id,
    receivable_id,
)
from .collections_flow import (  # noqa: F401 - historical import location
    collect_receivable,
    ensure_collection_case,
    reconcile_collection,
)


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    amount: float = 250.0,
    currency: str = "USD",
    instance_key: str | None = None,
) -> O2CEntities:
    if instance_key is not None and (not isinstance(instance_key, str) or not instance_key.strip()):
        raise ValueError("instance_key must be a nonempty string")
    if amount <= 0:
        raise ValueError("amount must be positive")
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
    order = context.entities.create(
        SalesOrder,
        key=(("o2c-reference", "order-1") if instance_key is None
             else ("o2c-reference", "order-1", instance_key)),
        state="submitted",
        attributes={"amount": float(amount), "currency": currency},
    )
    with persistence.transaction() as uow:
        uow.save_entity(order)
        uow.save_resource_definition(
            ResourceDefinition("fulfillment_team", capacity=1)
        )
        uow.save_resource_definition(
            ResourceDefinition("collection_agent", capacity=1)
        )
    return O2CEntities(order_id=order.id)


def _order(persistence: MemoryPersistence, order_id: str) -> SalesOrder:
    value = persistence.entity("sales_order", order_id)
    if value is None:
        raise RuntimeError(f"sales order was not persisted: {order_id}")
    return value


def _receivable(
    persistence: MemoryPersistence,
    order_id: str,
) -> Receivable | None:
    return persistence.entity("receivable", receivable_id(order_id))


def _collection_case(
    persistence: MemoryPersistence,
    receivable_id_value: str,
) -> CollectionCase | None:
    return persistence.entity(
        "collection_case",
        collection_case_id(receivable_id_value),
    )


def _dispatch(
    engine: Engine,
    entity,
    event: str,
    *,
    key: tuple[object, ...],
    correlation_id: str,
) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def reconcile_credit(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: O2CEntities,
) -> bool:
    order = _order(persistence, entities.order_id)
    available = bool(
        engine.context.scenarios.attribute("o2c.credit.available", True)
    )
    correlation_id = flow_correlation_id(order.id)

    if order.state == "submitted":
        event = "approve_credit" if available else "hold_credit"
        _dispatch(
            engine,
            order,
            event,
            key=("o2c", order.id, event),
            correlation_id=correlation_id,
        )
        return available

    if order.state == "credit_hold" and available:
        _dispatch(
            engine,
            order,
            "release_credit",
            key=("o2c", order.id, "release-credit"),
            correlation_id=correlation_id,
        )
        return True

    return order.state in {
        "ordered",
        "fulfilling",
        "partial_fulfillment",
        "fulfilled",
        "shipped",
        "invoiced",
    }


def reconcile_fulfillment(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: O2CEntities,
    partial: bool = False,
) -> bool:
    order = _order(persistence, entities.order_id)
    if order.state in {"fulfilled", "shipped", "invoiced"}:
        return True
    if order.state not in {"ordered", "fulfilling", "partial_fulfillment"}:
        return False

    request_id = f"fulfillment-team:{order.id}"
    available = bool(
        engine.context.scenarios.attribute("o2c.fulfillment.available", True)
    )
    if not available:
        engine.resources.withdraw(backend, request_id)
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="fulfillment_team",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    order = _order(persistence, order.id)
    correlation_id = flow_correlation_id(order.id)
    if order.state == "ordered":
        _dispatch(
            engine,
            order,
            "start_fulfillment",
            key=("o2c", order.id, "start-fulfillment"),
            correlation_id=correlation_id,
        )
        order = _order(persistence, order.id)

    if partial and order.state == "fulfilling":
        _dispatch(
            engine,
            order,
            "record_partial",
            key=("o2c", order.id, "partial-fulfillment"),
            correlation_id=correlation_id,
        )
        engine.resources.withdraw(backend, request_id)
        return False

    if order.state in {"fulfilling", "partial_fulfillment"}:
        _dispatch(
            engine,
            order,
            "fulfill",
            key=("o2c", order.id, "fulfill", order.version),
            correlation_id=correlation_id,
        )

    engine.resources.withdraw(backend, request_id)
    return _order(persistence, order.id).state == "fulfilled"


def ensure_receivable(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: O2CEntities,
) -> Receivable:
    existing = _receivable(persistence, entities.order_id)
    if existing is not None:
        return existing

    order = _order(persistence, entities.order_id)
    if order.state != "invoiced":
        raise RuntimeError(
            f"receivable requires SalesOrder(invoiced), got {order.state}"
        )

    value = engine.context.entities.create(
        Receivable,
        key=("o2c-reference", order.id, "receivable"),
        state="open",
        attributes={
            "order_id": order.id,
            "amount": float(order.attributes["amount"]),
            "currency": order.attributes["currency"],
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    return value


def ship_invoice_and_ensure_receivable(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: O2CEntities,
) -> Receivable:
    order = _order(persistence, entities.order_id)
    correlation_id = flow_correlation_id(order.id)
    if order.state == "fulfilled":
        _dispatch(
            engine,
            order,
            "ship",
            key=("o2c", order.id, "ship"),
            correlation_id=correlation_id,
        )
        order = _order(persistence, order.id)
    if order.state == "shipped":
        _dispatch(
            engine,
            order,
            "invoice",
            key=("o2c", order.id, "invoice"),
            correlation_id=correlation_id,
        )
    return ensure_receivable(
        persistence,
        engine,
        entities=entities,
    )


def schedule_due(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: O2CEntities,
    delay: timedelta = DUE_DELAY,
):
    receivable = ensure_receivable(
        persistence,
        engine,
        entities=entities,
    )
    if receivable.state != "open":
        return backend.now
    existing = engine.scheduler.find_pending(
        entity_type="receivable",
        entity_id=receivable.id,
        name="mark_due",
    )
    if existing is not None:
        return existing.work.due_at

    due_at = backend.now + delay
    command = engine.context.commands.create(
        "mark_due",
        target=receivable,
        due_at=due_at,
        correlation_id=flow_correlation_id(entities.order_id),
        key=("o2c", receivable.id, "mark-due"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def schedule_overdue(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: O2CEntities,
    delay: timedelta = OVERDUE_DELAY,
):
    receivable = _receivable(persistence, entities.order_id)
    if receivable is None:
        raise RuntimeError("receivable was not persisted")
    if receivable.state != "due":
        raise RuntimeError(f"overdue scheduling requires due, got {receivable.state}")
    existing = engine.scheduler.find_pending(
        entity_type="receivable",
        entity_id=receivable.id,
        name="mark_overdue",
    )
    if existing is not None:
        return existing.work.due_at

    due_at = backend.now + delay
    command = engine.context.commands.create(
        "mark_overdue",
        target=receivable,
        due_at=due_at,
        correlation_id=flow_correlation_id(entities.order_id),
        key=("o2c", receivable.id, "mark-overdue"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at
