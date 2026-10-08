from __future__ import annotations

from dataclasses import dataclass

from sose.persistence.base import Persistence

from .simulation import P2PEntities


@dataclass(frozen=True, slots=True)
class P2PProjection:
    """Read-only canonical projection of durable Procure-to-Pay truth."""

    requisition_id: str
    purchase_order_id: str
    receipt_id: str
    material_demand_id: str
    requisition_state: str
    purchase_order_state: str
    receipt_state: str
    material_demand_state: str
    sku: str
    quantity: float
    supplier: str
    inventory_level: float
    stocked: bool
    consumed: bool
    backordered: bool
    procure_to_consumption_seconds: float | None


@dataclass(frozen=True, slots=True)
class P2PKpis:
    """Canonical P2P KPIs derived from durable state and immutable events."""

    procure_to_consumption_seconds: float | None
    quantity: float
    transition_count: int
    supplier_delay_count: int
    partial_receipt_count: int
    rejected_receipt_count: int
    backorder_count: int
    consumed: bool


def _entity(persistence: Persistence, entity_type: str, entity_id: str):
    entity = persistence.entity(entity_type, entity_id)
    if entity is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return entity


def _inventory_level(persistence: Persistence) -> float:
    return next(
        (
            float(state.level)
            for state in persistence.container_states()
            if state.name == "inventory"
        ),
        0.0,
    )


def p2p_projection(
    persistence: Persistence,
    *,
    entities: P2PEntities,
) -> P2PProjection:
    """Project P2P process truth without mutating operational state."""

    requisition = _entity(persistence, "requisition", entities.requisition_id)
    purchase_order = _entity(
        persistence,
        "purchase_order",
        entities.purchase_order_id,
    )
    receipt = _entity(persistence, "receipt", entities.receipt_id)
    demand = _entity(
        persistence,
        "material_demand",
        entities.material_demand_id,
    )

    consumed = demand.state == "consumed"
    cycle_seconds = None
    if (
        consumed
        and requisition.created_at is not None
        and demand.updated_at is not None
    ):
        cycle_seconds = max(
            0.0,
            (demand.updated_at - requisition.created_at).total_seconds(),
        )

    return P2PProjection(
        requisition_id=requisition.id,
        purchase_order_id=purchase_order.id,
        receipt_id=receipt.id,
        material_demand_id=demand.id,
        requisition_state=requisition.state,
        purchase_order_state=purchase_order.state,
        receipt_state=receipt.state,
        material_demand_state=demand.state,
        sku=str(requisition.attributes["sku"]),
        quantity=float(requisition.attributes["quantity"]),
        supplier=str(purchase_order.attributes["supplier"]),
        inventory_level=_inventory_level(persistence),
        stocked=receipt.state == "stocked",
        consumed=consumed,
        backordered=demand.state == "backordered",
        procure_to_consumption_seconds=cycle_seconds,
    )


def p2p_kpis(
    persistence: Persistence,
    *,
    entities: P2PEntities,
) -> P2PKpis:
    """Return stable P2P KPIs from durable state and correlated event history."""

    projection = p2p_projection(persistence, entities=entities)
    process_entity_ids = {
        entities.requisition_id,
        entities.purchase_order_id,
        entities.receipt_id,
        entities.material_demand_id,
    }
    events = tuple(
        event
        for event in persistence.events()
        if event.entity_id in process_entity_ids
        and event.name == "entity.state_transition"
    )

    def count(entity_type: str, to_state: str) -> int:
        return sum(
            event.entity_type == entity_type
            and event.payload.get("to_state") == to_state
            for event in events
        )

    return P2PKpis(
        procure_to_consumption_seconds=projection.procure_to_consumption_seconds,
        quantity=projection.quantity,
        transition_count=len(events),
        supplier_delay_count=count("purchase_order", "delayed"),
        partial_receipt_count=count("receipt", "partial"),
        rejected_receipt_count=count("receipt", "rejected"),
        backorder_count=count("material_demand", "backordered"),
        consumed=projection.consumed,
    )
