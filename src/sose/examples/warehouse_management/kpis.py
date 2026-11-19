"""Shipment KPI projection for warehouse management (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime

from sose.persistence.base import Persistence

from .records import WarehouseManagementEntities, _entity


def _iso_attribute(entity, key: str) -> datetime | None:
    return datetime.fromisoformat(str(raw)) if (raw := entity.attributes.get(key)) else None


def shipment_kpis(
    persistence: Persistence,
    *,
    entities: WarehouseManagementEntities,
) -> dict[str, object]:
    shipment = _entity(persistence, "warehouse_management_shipment", entities.shipment_id)
    departed_at = _iso_attribute(shipment, "departed_at")
    completed_at = _iso_attribute(shipment, "completed_at")
    planned_at = _iso_attribute(shipment, "planned_completion_at")
    completed = shipment.state == "completed" and completed_at is not None
    lead_time = (
        (completed_at - departed_at).total_seconds()
        if completed_at is not None and departed_at is not None
        else None
    )
    lateness = (
        max(0.0, (completed_at - planned_at).total_seconds())
        if completed_at is not None and planned_at is not None
        else None
    )
    return {
        "shipment_state": shipment.state,
        "completed": completed,
        "on_time": (
            completed_at <= planned_at
            if completed_at is not None and planned_at is not None
            else None
        ),
        "lead_time_seconds": lead_time,
        "lateness_seconds": lateness,
        "assigned_dock_id": shipment.attributes.get("assigned_dock_id"),
        "assigned_forklift_id": shipment.attributes.get("assigned_forklift_id"),
    }
