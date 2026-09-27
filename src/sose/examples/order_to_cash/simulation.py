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

from .entities import CollectionCase, Receivable, SalesOrder
from .scenarios import ORIGIN
from .statecharts import CollectionCaseChart, ReceivableChart, SalesOrderChart


DUE_DELAY = timedelta(hours=2)
OVERDUE_DELAY = timedelta(hours=2)
COLLECTION_FOLLOWUP_DELAY = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class O2CEntities:
    order_id: str


def flow_correlation_id(order_id: str) -> str:
    return deterministic_id("o2c-flow", order_id)


def receivable_id(order_id: str) -> str:
    return deterministic_id(
        "entity",
        "receivable",
        "o2c-reference",
        order_id,
        "receivable",
    )


def collection_case_id(receivable_id_value: str) -> str:
    return deterministic_id(
        "entity",
        "collection_case",
        "o2c-reference",
        receivable_id_value,
        "collection-case",
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
        random=RandomSource(root_seed=336),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("sales_order", SalesOrderChart))
    registry.register(EntityType("receivable", ReceivableChart))
    registry.register(EntityType("collection_case", CollectionCaseChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    amount: float = 250.0,
) -> O2CEntities:
    if amount <= 0:
        raise ValueError("amount must be positive")
    context, _ = build_runtime(persistence)
    order = context.entities.create(
        SalesOrder,
        key=("o2c-reference", "order-1"),
        state="submitted",
        attributes={"amount": float(amount), "currency": "USD"},
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


def _request_exists(persistence: MemoryPersistence, request_id: str) -> bool:
    return any(
        d.request_id == request_id for d in persistence.resource_demands()
    ) or any(
        r.request_id == request_id
        for r in persistence.resource_reservations()
    )


def _reservation(persistence: MemoryPersistence, request_id: str):
    return next(
        (
            r for r in persistence.resource_reservations()
            if r.request_id == request_id
        ),
        None,
    )


def _request_resource(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    resource_name: str,
    request_id: str,
):
    if not _request_exists(persistence, request_id):
        engine.resources.request(
            backend,
            resource_name=resource_name,
            request_id=request_id,
            requested_at=backend.now,
            priority=100,
        )
    backend.run_until(backend.now)
    return _reservation(persistence, request_id)


def _release(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    request_id: str,
) -> None:
    reservation = _reservation(persistence, request_id)
    if reservation is not None:
        engine.resources.release(backend, reservation.reservation_id)
        backend.run_until(backend.now)


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
        engine.resources.cancel_pending(backend, request_id)
        _release(
            persistence,
            engine,
            backend,
            request_id=request_id,
        )
        backend.run_until(backend.now)
        return False

    reservation = _request_resource(
        persistence,
        engine,
        backend,
        resource_name="fulfillment_team",
        request_id=request_id,
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
        _release(
            persistence,
            engine,
            backend,
            request_id=request_id,
        )
        return False

    if order.state in {"fulfilling", "partial_fulfillment"}:
        _dispatch(
            engine,
            order,
            "fulfill",
            key=("o2c", order.id, "fulfill", order.version),
            correlation_id=correlation_id,
        )

    _release(
        persistence,
        engine,
        backend,
        request_id=request_id,
    )
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


def _scheduled_command(
    persistence: MemoryPersistence,
    *,
    entity_type: str,
    entity_id: str,
    name: str,
):
    for work in persistence.scheduled_work():
        command = persistence.command(work.command_id)
        if (
            command is not None
            and command.entity_type == entity_type
            and command.entity_id == entity_id
            and command.name == name
        ):
            return work, command
    return None


def _cancel_scheduled_command(
    persistence: MemoryPersistence,
    *,
    entity_type: str,
    entity_id: str,
    name: str,
) -> bool:
    found = _scheduled_command(
        persistence,
        entity_type=entity_type,
        entity_id=entity_id,
        name=name,
    )
    if found is None:
        return False
    work, command = found
    with persistence.transaction() as uow:
        persisted = uow.get_scheduled_work(work.work_id)
        if persisted != work:
            return False
        uow.delete_scheduled_work(work.work_id)
        uow.delete_command(command.command_id)
    return True


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
    existing = _scheduled_command(
        persistence,
        entity_type="receivable",
        entity_id=receivable.id,
        name="mark_due",
    )
    if existing is not None:
        return existing[0].due_at

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
    existing = _scheduled_command(
        persistence,
        entity_type="receivable",
        entity_id=receivable.id,
        name="mark_overdue",
    )
    if existing is not None:
        return existing[0].due_at

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


def ensure_collection_case(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: O2CEntities,
) -> CollectionCase:
    receivable = _receivable(persistence, entities.order_id)
    if receivable is None:
        raise RuntimeError("receivable was not persisted")
    existing = _collection_case(persistence, receivable.id)
    if existing is not None:
        return existing
    if receivable.state != "overdue":
        raise RuntimeError(
            f"collection case requires Receivable(overdue), got {receivable.state}"
        )

    value = engine.context.entities.create(
        CollectionCase,
        key=("o2c-reference", receivable.id, "collection-case"),
        state="opened",
        attributes={"receivable_id": receivable.id},
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    return value


def reconcile_collection(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: O2CEntities,
    promise: bool = False,
) -> bool:
    case = ensure_collection_case(
        persistence,
        engine,
        entities=entities,
    )
    request_id = f"collection-agent:{case.id}"
    reservation = _request_resource(
        persistence,
        engine,
        backend,
        resource_name="collection_agent",
        request_id=request_id,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(entities.order_id)
    case = _collection_case(
        persistence,
        str(case.attributes["receivable_id"]),
    )
    if case is None:
        raise RuntimeError("collection case disappeared")
    if case.state == "opened":
        _dispatch(
            engine,
            case,
            "assign",
            key=("o2c-collection", case.id, "assign"),
            correlation_id=correlation_id,
        )
        case = persistence.entity("collection_case", case.id)
    if case is not None and case.state == "assigned":
        _dispatch(
            engine,
            case,
            "contact",
            key=("o2c-collection", case.id, "contact"),
            correlation_id=correlation_id,
        )
        case = persistence.entity("collection_case", case.id)

    if promise and case is not None and case.state == "contacted":
        _dispatch(
            engine,
            case,
            "promise",
            key=("o2c-collection", case.id, "promise"),
            correlation_id=correlation_id,
        )
        case = persistence.entity("collection_case", case.id)
        due_at = backend.now + COLLECTION_FOLLOWUP_DELAY
        command = engine.context.commands.create(
            "escalate",
            target=case,
            due_at=due_at,
            correlation_id=correlation_id,
            key=("o2c-collection", case.id, "followup"),
        )
        engine.context.schedules.at(due_at, command=command)

    _release(
        persistence,
        engine,
        backend,
        request_id=request_id,
    )
    return True


def collect_receivable(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: O2CEntities,
) -> bool:
    receivable = _receivable(persistence, entities.order_id)
    if receivable is None:
        return False
    if receivable.state == "collected":
        return True
    if receivable.state not in {"due", "overdue"}:
        return False

    _dispatch(
        engine,
        receivable,
        "collect",
        key=("o2c", receivable.id, "collect"),
        correlation_id=flow_correlation_id(entities.order_id),
    )
    _cancel_scheduled_command(
        persistence,
        entity_type="receivable",
        entity_id=receivable.id,
        name="mark_overdue",
    )

    case = _collection_case(persistence, receivable.id)
    if case is not None and case.state in {"contacted", "promised", "escalated"}:
        _dispatch(
            engine,
            case,
            "resolve",
            key=("o2c-collection", case.id, "resolve"),
            correlation_id=flow_correlation_id(entities.order_id),
        )
        _cancel_scheduled_command(
            persistence,
            entity_type="collection_case",
            entity_id=case.id,
            name="escalate",
        )
    return True
