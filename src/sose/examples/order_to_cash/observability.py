from __future__ import annotations

from dataclasses import dataclass

from sose.persistence.base import Persistence

from .simulation import (
    O2CEntities,
    collection_case_id,
    flow_correlation_id,
    receivable_id,
)


@dataclass(frozen=True, slots=True)
class OrderToCashProjection:
    """Read-only canonical projection of durable Order-to-Cash truth."""

    order_id: str
    receivable_id: str | None
    collection_case_id: str | None
    order_state: str
    receivable_state: str | None
    collection_case_state: str | None
    amount: float
    currency: str
    collected: bool
    overdue: bool
    collection_open: bool
    order_to_cash_seconds: float | None


@dataclass(frozen=True, slots=True)
class OrderToCashKpis:
    """Canonical O2C KPIs derived from durable entities and immutable events."""

    cash_collection: bool
    order_to_cash_seconds: float | None
    amount: float
    transition_count: int
    credit_hold_count: int
    partial_fulfillment_count: int
    overdue_count: int
    collection_case_count: int
    collection_escalation_count: int


def order_to_cash_projection(
    persistence: Persistence,
    *,
    entities: O2CEntities,
) -> OrderToCashProjection:
    """Project O2C process truth without mutating operational state."""

    order = persistence.entity("sales_order", entities.order_id)
    if order is None:
        raise RuntimeError("sales order was not persisted")

    receivable_key = receivable_id(order.id)
    receivable = persistence.entity("receivable", receivable_key)
    case = None
    case_key = None
    if receivable is not None:
        case_key = collection_case_id(receivable.id)
        case = persistence.entity("collection_case", case_key)

    collected = receivable is not None and receivable.state == "collected"
    order_to_cash_seconds = None
    if (
        collected
        and order.created_at is not None
        and receivable is not None
        and receivable.updated_at is not None
    ):
        order_to_cash_seconds = max(
            0.0,
            (receivable.updated_at - order.created_at).total_seconds(),
        )

    return OrderToCashProjection(
        order_id=order.id,
        receivable_id=receivable.id if receivable is not None else None,
        collection_case_id=case.id if case is not None else None,
        order_state=order.state,
        receivable_state=receivable.state if receivable is not None else None,
        collection_case_state=case.state if case is not None else None,
        amount=float(order.attributes["amount"]),
        currency=str(order.attributes["currency"]),
        collected=collected,
        overdue=receivable is not None and receivable.state == "overdue",
        collection_open=case is not None and case.state != "resolved",
        order_to_cash_seconds=order_to_cash_seconds,
    )


def order_to_cash_kpis(
    persistence: Persistence,
    *,
    entities: O2CEntities,
) -> OrderToCashKpis:
    """Return stable O2C KPIs from durable state and correlated event history."""

    projection = order_to_cash_projection(persistence, entities=entities)
    events = tuple(
        event
        for event in persistence.events()
        if event.correlation_id == flow_correlation_id(entities.order_id)
        and event.name == "entity.state_transition"
    )

    return OrderToCashKpis(
        cash_collection=projection.collected,
        order_to_cash_seconds=projection.order_to_cash_seconds,
        amount=projection.amount,
        transition_count=len(events),
        credit_hold_count=sum(
            event.payload.get("to_state") == "credit_hold" for event in events
        ),
        partial_fulfillment_count=sum(
            event.payload.get("to_state") == "partial_fulfillment"
            for event in events
        ),
        overdue_count=sum(
            event.payload.get("to_state") == "overdue" for event in events
        ),
        collection_case_count=int(projection.collection_case_id is not None),
        collection_escalation_count=sum(
            event.entity_type == "collection_case"
            and event.payload.get("to_state") == "escalated"
            for event in events
        ),
    )
