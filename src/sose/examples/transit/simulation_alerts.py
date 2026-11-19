"""Transit trip cancellation and service alerts (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.persistence.memory import MemoryPersistence

from .entities import (
    ServiceAlert,
)
from .simulation_runtime import (
    _at,
    _dispatch,
    _entity,
    _trip,
    _vehicle,
    flow_correlation_id,
    service_alert_id,
)

if TYPE_CHECKING:
    from .simulation import TransitEntities


def cancel_trip(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TransitEntities,
    trip_id: str,
) -> bool:
    trip = _trip(persistence, trip_id)
    if trip.state == "cancelled":
        return True
    if trip.state == "completed":
        return False

    for name in ("start", "complete"):
        engine.scheduler.cancel_pending(
            entity_type="transit_scheduled_trip",
            entity_id=trip.id,
            name=name,
        )
    _dispatch(
        engine,
        trip,
        "cancel",
        key=("transit-trip", trip.id, "cancel"),
        correlation_id=flow_correlation_id(str(trip.attributes["block_id"])),
    )

    vehicle = _vehicle(persistence, entities)
    if vehicle.attributes.get("active_trip_id") == trip.id:
        if vehicle.state == "in_service":
            _dispatch(
                engine,
                vehicle,
                "release",
                key=("transit-vehicle", vehicle.id, trip.id, "cancel-release"),
                correlation_id=flow_correlation_id(str(trip.attributes["block_id"])),
            )
            vehicle = _vehicle(persistence, entities)
        vehicle.attributes["active_trip_id"] = None
        with persistence.transaction() as uow:
            uow.save_entity(vehicle)
    return True


def schedule_service_alert(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    alert_key: str,
    affected_trip_ids: tuple[str, ...],
    start_at: datetime,
    end_at: datetime,
) -> ServiceAlert:
    requested = _validate_and_resolve_alert_scope(
        persistence,
        alert_key=alert_key,
        affected_trip_ids=affected_trip_ids,
        start_at=start_at,
        end_at=end_at,
    )
    alert, start_at, end_at = _load_or_create_service_alert(
        persistence,
        engine,
        alert_key=alert_key,
        requested=requested,
        start_at=start_at,
        end_at=end_at,
    )
    _schedule_alert_boundary_if_missing(
        engine,
        alert=alert,
        name="activate",
        due_at=start_at,
        allowed_states={"scheduled"},
    )
    _schedule_alert_boundary_if_missing(
        engine,
        alert=alert,
        name="clear",
        due_at=end_at,
        allowed_states={"scheduled", "active"},
    )

    return _entity(persistence, "transit_service_alert", alert.id)


def _validate_and_resolve_alert_scope(
    persistence: MemoryPersistence,
    *,
    alert_key: str,
    affected_trip_ids: tuple[str, ...],
    start_at: datetime,
    end_at: datetime,
) -> tuple[str, ...]:
    if not alert_key:
        raise ValueError("alert_key must be non-empty")
    if not affected_trip_ids:
        raise ValueError("service alert requires affected trips")
    if end_at <= start_at:
        raise ValueError("service alert end must be after start")
    for trip_id in affected_trip_ids:
        _trip(persistence, trip_id)
    return tuple(dict.fromkeys(affected_trip_ids))


def _load_or_create_service_alert(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    alert_key: str,
    requested: tuple[str, ...],
    start_at: datetime,
    end_at: datetime,
) -> tuple[ServiceAlert, datetime, datetime]:
    alert = persistence.entity("transit_service_alert", service_alert_id(alert_key))
    if alert is None:
        alert = engine.context.entities.create(
            ServiceAlert,
            key=("transit-reference", "alert", alert_key),
            state="scheduled",
            attributes={
                "alert_key": alert_key,
                "affected_trip_ids": list(requested),
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(alert)
        return alert, start_at, end_at

    if tuple(alert.attributes["affected_trip_ids"]) != requested:
        raise ValueError("service alert scope cannot change on replay")
    persisted_start = _at(alert.attributes["start_at"])
    persisted_end = _at(alert.attributes["end_at"])
    if start_at != persisted_start or end_at != persisted_end:
        raise ValueError("service alert time range cannot change on replay")
    return alert, persisted_start, persisted_end


def _schedule_alert_boundary_if_missing(
    engine: Engine,
    *,
    alert: ServiceAlert,
    name: str,
    due_at: datetime,
    allowed_states: set[str],
) -> None:
    if alert.state not in allowed_states:
        return
    if engine.scheduler.find_pending(
        entity_type="transit_service_alert",
        entity_id=alert.id,
        name=name,
    ) is not None:
        return
    command = engine.context.commands.create(
        name,
        target=alert,
        due_at=due_at,
        correlation_id=deterministic_id("transit-alert", alert.id),
        key=("transit-alert", alert.id, name),
    )
    engine.context.schedules.at(due_at, command=command)


def cancel_service_alert(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    alert_key: str,
) -> bool:
    alert = _entity(
        persistence,
        "transit_service_alert",
        service_alert_id(alert_key),
    )
    if alert.state == "cancelled":
        return True
    if alert.state == "cleared":
        return False

    for name in ("activate", "clear"):
        engine.scheduler.cancel_pending(
            entity_type="transit_service_alert",
            entity_id=alert.id,
            name=name,
        )
    _dispatch(
        engine,
        alert,
        "cancel",
        key=("transit-alert", alert.id, "cancel", alert.version),
        correlation_id=deterministic_id("transit-alert", alert.id),
    )
    return True
