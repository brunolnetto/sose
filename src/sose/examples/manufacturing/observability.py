from __future__ import annotations

from dataclasses import dataclass

from sose.persistence.base import Persistence

from .simulation import ManufacturingEntities, flow_correlation_id


@dataclass(frozen=True, slots=True)
class ManufacturingProjection:
    """Read-only canonical projection of durable Manufacturing truth."""

    production_order_id: str
    operation_id: str
    order_state: str
    operation_state: str
    completed: bool
    planned_quantity: float
    raw_material_quantity: float
    finished_goods_quantity: float
    wip_item_count: int
    lead_time_seconds: float | None


@dataclass(frozen=True, slots=True)
class ManufacturingKpis:
    """Canonical Manufacturing KPI projection derived from persisted facts."""

    completed: bool
    lead_time_seconds: float | None
    output_quantity: float
    yield_ratio: float | None
    transition_count: int
    rework_count: int
    breakdown_count: int


def manufacturing_projection(
    persistence: Persistence,
    *,
    entities: ManufacturingEntities,
) -> ManufacturingProjection:
    """Project durable process truth without mutating operational state."""

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    container_levels = {
        state.name: float(state.level) for state in persistence.container_states()
    }
    planned_quantity = float(order.attributes["quantity"])
    completed = order.state == "completed"
    lead_time_seconds = None
    if completed and order.created_at is not None and order.updated_at is not None:
        lead_time_seconds = max(
            0.0,
            (order.updated_at - order.created_at).total_seconds(),
        )

    return ManufacturingProjection(
        production_order_id=order.id,
        operation_id=operation.id,
        order_state=order.state,
        operation_state=operation.state,
        completed=completed,
        planned_quantity=planned_quantity,
        raw_material_quantity=container_levels.get("raw_material", 0.0),
        finished_goods_quantity=container_levels.get("finished_goods", 0.0),
        wip_item_count=sum(
            item.store_name == "wip_buffer" for item in persistence.store_items()
        ),
        lead_time_seconds=lead_time_seconds,
    )


def manufacturing_kpis(
    persistence: Persistence,
    *,
    entities: ManufacturingEntities,
) -> ManufacturingKpis:
    """Return stable KPIs from the canonical projection and immutable event history."""

    projection = manufacturing_projection(persistence, entities=entities)
    events = tuple(
        event
        for event in persistence.events()
        if event.correlation_id == flow_correlation_id()
        and event.name == "entity.state_transition"
    )
    output = projection.finished_goods_quantity
    yield_ratio = (
        output / projection.planned_quantity
        if projection.completed and projection.planned_quantity > 0.0
        else None
    )

    return ManufacturingKpis(
        completed=projection.completed,
        lead_time_seconds=projection.lead_time_seconds,
        output_quantity=output,
        yield_ratio=yield_ratio,
        transition_count=len(events),
        rework_count=sum(
            event.payload.get("trigger") == "rework_order"
            or event.payload.get("to_state") == "rework"
            for event in events
        ),
        breakdown_count=sum(
            event.payload.get("trigger") == "breakdown"
            or event.payload.get("to_state") == "machine_down"
            for event in events
        ),
    )
