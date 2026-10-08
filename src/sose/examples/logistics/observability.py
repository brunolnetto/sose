from __future__ import annotations

from dataclasses import dataclass

from sose.persistence.base import Persistence

from .simulation import LogisticsEntities


_TERMINAL_OUTCOMES = frozenset({"delivered", "lost", "damaged", "returned"})


@dataclass(frozen=True, slots=True)
class LogisticsProjection:
    """Read-only canonical projection of durable Logistics truth."""

    shipment_id: str
    shipment_state: str
    service_level: str
    route: str
    delivered: bool
    terminal_outcome: str | None
    shipment_lead_time_seconds: float | None


@dataclass(frozen=True, slots=True)
class LogisticsKpis:
    """Canonical Logistics KPIs derived from durable state and immutable events."""

    shipment_lead_time_seconds: float | None
    transition_count: int
    delay_count: int
    delivery_attempt_count: int
    failed_attempt_count: int
    delivered_attempt_count: int
    delivered: bool


def logistics_projection(
    persistence: Persistence,
    *,
    entities: LogisticsEntities,
) -> LogisticsProjection:
    """Project Shipment truth without mutating the operational process."""

    shipment = persistence.entity("shipment", entities.shipment_id)
    if shipment is None:
        raise RuntimeError(f"shipment was not persisted: {entities.shipment_id}")

    terminal_outcome = (
        shipment.state if shipment.state in _TERMINAL_OUTCOMES else None
    )
    lead_time = None
    if (
        terminal_outcome is not None
        and shipment.created_at is not None
        and shipment.updated_at is not None
    ):
        lead_time = max(
            0.0,
            (shipment.updated_at - shipment.created_at).total_seconds(),
        )

    return LogisticsProjection(
        shipment_id=shipment.id,
        shipment_state=shipment.state,
        service_level=str(shipment.attributes["service_level"]),
        route=str(shipment.attributes["route"]),
        delivered=shipment.state == "delivered",
        terminal_outcome=terminal_outcome,
        shipment_lead_time_seconds=lead_time,
    )


def logistics_kpis(
    persistence: Persistence,
    *,
    entities: LogisticsEntities,
) -> LogisticsKpis:
    """Return stable Logistics KPIs from correlated immutable event history."""

    projection = logistics_projection(persistence, entities=entities)
    def belongs_to_shipment(event) -> bool:
        if event.entity_type == "shipment":
            return event.entity_id == entities.shipment_id
        if event.entity_type != "delivery_attempt":
            return False
        attempt = persistence.entity("delivery_attempt", event.entity_id)
        return (
            attempt is not None
            and attempt.attributes.get("shipment_id") == entities.shipment_id
        )

    events = tuple(
        event
        for event in persistence.events()
        if event.name == "entity.state_transition"
        and belongs_to_shipment(event)
    )
    attempts = {
        event.entity_id
        for event in events
        if event.entity_type == "delivery_attempt"
    }

    return LogisticsKpis(
        shipment_lead_time_seconds=projection.shipment_lead_time_seconds,
        transition_count=len(events),
        delay_count=sum(
            event.entity_type == "shipment"
            and str(event.payload.get("to_state", "")).startswith("delayed_")
            for event in events
        ),
        delivery_attempt_count=len(attempts),
        failed_attempt_count=sum(
            event.entity_type == "delivery_attempt"
            and event.payload.get("to_state") == "failed"
            for event in events
        ),
        delivered_attempt_count=sum(
            event.entity_type == "delivery_attempt"
            and event.payload.get("to_state") == "delivered"
            for event in events
        ),
        delivered=projection.delivered,
    )
