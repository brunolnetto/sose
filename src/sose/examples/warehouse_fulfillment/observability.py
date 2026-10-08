from __future__ import annotations

from dataclasses import dataclass

from sose.persistence.base import Persistence

from .simulation import WarehouseEntities, flow_correlation_id


@dataclass(frozen=True, slots=True)
class WarehouseFulfillmentProjection:
    """Read-only projection of durable Warehouse Fulfillment process truth."""

    order_id: str
    order_state: str
    completed: bool
    requested_quantity: float
    allocation_count: int
    allocated_quantity: float
    picked_quantity: float
    substitution_allocation_count: int
    inventory_occurrence_count: int
    lead_time_seconds: float | None


@dataclass(frozen=True, slots=True)
class WarehouseFulfillmentKpis:
    """Stable KPIs derived only from persisted fulfillment facts."""

    completion: bool
    lead_time_seconds: float | None
    fulfilled_quantity: float
    fill_rate: float
    transition_count: int
    substitution_count: int
    correction_count: int


def warehouse_fulfillment_projection(
    persistence: Persistence,
    *,
    entities: WarehouseEntities,
) -> WarehouseFulfillmentProjection:
    order = persistence.entity(
        "warehouse_fulfillment_order",
        entities.order_id,
    )
    if order is None:
        raise RuntimeError("warehouse fulfillment order was not persisted")

    allocation_ids = tuple(
        str(value) for value in order.attributes.get("allocation_ids", ())
    )
    allocations = []
    for allocation_id in allocation_ids:
        allocation = persistence.entity("warehouse_allocation", allocation_id)
        if allocation is None:
            raise RuntimeError(
                "warehouse fulfillment allocation index references missing durable allocation"
            )
        allocations.append(allocation)

    requested_quantity = float(order.attributes["requested_quantity"])
    allocated_quantity = sum(
        float(allocation.attributes["quantity"])
        for allocation in allocations
    )
    picked_quantity = sum(
        float(allocation.attributes["quantity"])
        for allocation in allocations
        if allocation.state in {"picked", "shipped"}
    )
    occurrence_ids = tuple(
        str(value) for value in order.attributes.get("occurrence_ids", ())
    )
    completed = order.state == "shipped"
    lead_time_seconds = None
    if completed and order.created_at is not None and order.updated_at is not None:
        lead_time_seconds = max(
            0.0,
            (order.updated_at - order.created_at).total_seconds(),
        )

    return WarehouseFulfillmentProjection(
        order_id=order.id,
        order_state=order.state,
        completed=completed,
        requested_quantity=requested_quantity,
        allocation_count=len(allocations),
        allocated_quantity=allocated_quantity,
        picked_quantity=picked_quantity,
        substitution_allocation_count=sum(
            bool(allocation.attributes.get("substituted", False))
            for allocation in allocations
        ),
        inventory_occurrence_count=len(occurrence_ids),
        lead_time_seconds=lead_time_seconds,
    )


def warehouse_fulfillment_kpis(
    persistence: Persistence,
    *,
    entities: WarehouseEntities,
) -> WarehouseFulfillmentKpis:
    projection = warehouse_fulfillment_projection(
        persistence,
        entities=entities,
    )
    events = tuple(
        event
        for event in persistence.events()
        if event.correlation_id == flow_correlation_id(entities.order_id)
        and event.name == "entity.state_transition"
    )
    correction_count = sum(
        entity.entity_type == "warehouse_inventory_occurrence"
        and entity.attributes.get("kind") == "correction"
        for entity in persistence.entities()
    )
    fulfilled_quantity = projection.picked_quantity if projection.completed else 0.0
    fill_rate = (
        fulfilled_quantity / projection.requested_quantity
        if projection.requested_quantity > 0.0
        else 0.0
    )

    return WarehouseFulfillmentKpis(
        completion=projection.completed,
        lead_time_seconds=projection.lead_time_seconds,
        fulfilled_quantity=fulfilled_quantity,
        fill_rate=fill_rate,
        transition_count=len(events),
        substitution_count=projection.substitution_allocation_count,
        correction_count=correction_count,
    )
