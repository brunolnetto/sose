from __future__ import annotations

from dataclasses import dataclass

from sose.persistence.base import Persistence

from .simulation import MROEntities, flow_correlation_id


@dataclass(frozen=True, slots=True)
class MROProjection:
    """Read-only canonical projection of durable MRO truth."""

    work_order_id: str
    part_demand_id: str
    work_order_state: str
    part_demand_state: str
    closed: bool
    cancelled: bool
    planned_quantity: float
    remaining_spare_parts: float
    spare_part_lot_count: int
    lead_time_seconds: float | None


@dataclass(frozen=True, slots=True)
class MROKpis:
    """Canonical MRO KPIs derived from persisted state and immutable events."""

    closure: bool
    lead_time_seconds: float | None
    parts_consumed: float
    remaining_spare_parts: float
    transition_count: int
    material_wait_count: int
    resource_wait_count: int
    interruption_count: int


def mro_projection(
    persistence: Persistence,
    *,
    entities: MROEntities,
) -> MROProjection:
    """Project durable MRO process truth without mutating operational state."""

    work_order = persistence.entity("work_order", entities.work_order_id)
    part_demand = persistence.entity("part_demand", entities.part_demand_id)
    if work_order is None or part_demand is None:
        raise RuntimeError("MRO entities were not persisted")

    container_levels = {
        state.name: float(state.level) for state in persistence.container_states()
    }
    closed = work_order.state == "closed"
    cancelled = work_order.state == "cancelled"
    lead_time_seconds = None
    if closed and work_order.created_at is not None and work_order.updated_at is not None:
        lead_time_seconds = max(
            0.0,
            (work_order.updated_at - work_order.created_at).total_seconds(),
        )

    return MROProjection(
        work_order_id=work_order.id,
        part_demand_id=part_demand.id,
        work_order_state=work_order.state,
        part_demand_state=part_demand.state,
        closed=closed,
        cancelled=cancelled,
        planned_quantity=float(work_order.attributes["quantity"]),
        remaining_spare_parts=container_levels.get("spare_parts", 0.0),
        spare_part_lot_count=sum(
            item.store_name == "spare_part_lots" for item in persistence.store_items()
        ),
        lead_time_seconds=lead_time_seconds,
    )


def mro_kpis(
    persistence: Persistence,
    *,
    entities: MROEntities,
) -> MROKpis:
    """Return stable maintenance KPIs from the canonical durable projection."""

    projection = mro_projection(persistence, entities=entities)
    events = tuple(
        event
        for event in persistence.events()
        if event.correlation_id == flow_correlation_id()
        and event.name == "entity.state_transition"
    )
    parts_consumed = sum(
        float(result.amount)
        for result in persistence.container_operation_results()
        if result.request_id == "consume-spare-part-1"
    )

    return MROKpis(
        closure=projection.closed,
        lead_time_seconds=projection.lead_time_seconds,
        parts_consumed=parts_consumed,
        remaining_spare_parts=projection.remaining_spare_parts,
        transition_count=len(events),
        material_wait_count=sum(
            event.payload.get("trigger") == "wait_for_material"
            or event.payload.get("to_state") == "waiting_material"
            for event in events
        ),
        resource_wait_count=sum(
            event.payload.get("trigger") == "wait_for_resource"
            or event.payload.get("to_state") == "waiting_resource"
            for event in events
        ),
        interruption_count=sum(
            event.payload.get("trigger") == "interrupt"
            or event.payload.get("to_state") == "interrupted"
            for event in events
        ),
    )
