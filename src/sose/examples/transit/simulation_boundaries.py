"""Transit trip boundary scheduling and vehicle reconciliation (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import (
    ScheduledTrip,
)
from .simulation_runtime import (
    _at,
    _dispatch,
    _trip,
    _vehicle,
    flow_correlation_id,
)

if TYPE_CHECKING:
    from .simulation import TransitEntities


def _ensure_boundary(
    engine: Engine,
    *,
    trip: ScheduledTrip,
    name: str,
    due_at: datetime,
) -> None:
    if engine.scheduler.find_pending(
        entity_type="transit_scheduled_trip",
        entity_id=trip.id,
        name=name,
    ) is not None:
        return
    command = engine.context.commands.create(
        name,
        target=trip,
        due_at=due_at,
        correlation_id=flow_correlation_id(str(trip.attributes["block_id"])),
        key=(
            "transit-trip",
            trip.id,
            name,
            due_at.isoformat(),
            int(trip.attributes.get("projection_revision", 0)),
        ),
    )
    engine.context.schedules.at(due_at, command=command)


def ensure_trip_boundaries(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    trip_id: str,
) -> tuple[datetime | None, datetime | None]:
    trip = _trip(persistence, trip_id)
    start_at = _at(trip.attributes["projected_start_at"])
    end_at = _at(trip.attributes["projected_end_at"])

    if trip.state == "planned":
        _ensure_boundary(engine, trip=trip, name="start", due_at=start_at)
        _ensure_boundary(engine, trip=trip, name="complete", due_at=end_at)
        return start_at, end_at
    if trip.state == "running":
        _ensure_boundary(engine, trip=trip, name="complete", due_at=end_at)
        return None, end_at
    return None, None


def schedule_reference_block(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TransitEntities,
) -> None:
    ensure_trip_boundaries(
        persistence,
        engine,
        trip_id=entities.trip_a_id,
    )
    ensure_trip_boundaries(
        persistence,
        engine,
        trip_id=entities.trip_b_id,
    )


def _sync_trip_boundary(
    engine: Engine,
    *,
    trip: ScheduledTrip,
    name: str,
    due_at: datetime,
) -> None:
    existing = engine.scheduler.find_pending(
        entity_type="transit_scheduled_trip",
        entity_id=trip.id,
        name=name,
    )
    if existing is not None and existing.work.due_at == due_at:
        return
    if existing is not None:
        engine.scheduler.cancel(existing.work.work_id)
    _ensure_boundary(engine, trip=trip, name=name, due_at=due_at)


def _reschedule_trip_boundaries(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: TransitEntities,
    trip_id: str,
) -> None:
    trip = _trip(persistence, trip_id)
    start_at = _at(trip.attributes["projected_start_at"])
    end_at = _at(trip.attributes["projected_end_at"])

    if trip.state == "planned":
        # A realtime correction may arrive after the newly projected boundary.
        # In that case the domain catches up at the backend's current logical
        # time rather than creating durable work in the past.
        effective_start = max(start_at, backend.now)
        effective_end = max(end_at, effective_start)
        _sync_trip_boundary(
            engine,
            trip=trip,
            name="start",
            due_at=effective_start,
        )
        _sync_trip_boundary(
            engine,
            trip=trip,
            name="complete",
            due_at=effective_end,
        )
        return

    engine.scheduler.cancel_pending(
        entity_type="transit_scheduled_trip",
        entity_id=trip.id,
        name="start",
    )
    if trip.state == "running":
        if end_at <= backend.now:
            engine.scheduler.cancel_pending(
                entity_type="transit_scheduled_trip",
                entity_id=trip.id,
                name="complete",
            )
            _dispatch(
                engine,
                trip,
                "complete",
                key=("transit-trip", trip.id, "complete-catch-up", backend.now.isoformat()),
                correlation_id=flow_correlation_id(str(trip.attributes["block_id"])),
            )
            reconcile_vehicle_for_trip(
                persistence,
                engine,
                entities=entities,
                trip_id=trip.id,
            )
            return
        _sync_trip_boundary(
            engine,
            trip=trip,
            name="complete",
            due_at=end_at,
        )
        return

    engine.scheduler.cancel_pending(
        entity_type="transit_scheduled_trip",
        entity_id=trip.id,
        name="complete",
    )


def reconcile_vehicle_for_trip(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TransitEntities,
    trip_id: str,
) -> bool:
    trip = _trip(persistence, trip_id)
    vehicle = _vehicle(persistence, entities)
    correlation_id = flow_correlation_id(str(trip.attributes["block_id"]))

    if trip.state == "running":
        active_trip_id = vehicle.attributes.get("active_trip_id")
        if active_trip_id not in {None, trip.id}:
            raise RuntimeError(
                f"vehicle already serves another trip: {active_trip_id}"
            )
        if vehicle.state == "available":
            _dispatch(
                engine,
                vehicle,
                "assign",
                key=("transit-vehicle", vehicle.id, trip.id, "assign"),
                correlation_id=correlation_id,
            )
            vehicle = _vehicle(persistence, entities)
        if vehicle.state != "in_service":
            raise RuntimeError(
                f"running trip requires vehicle in_service, got {vehicle.state}"
            )
        if vehicle.attributes.get("active_trip_id") != trip.id:
            vehicle.attributes["active_trip_id"] = trip.id
            with persistence.transaction() as uow:
                uow.save_entity(vehicle)
        return True

    if trip.state in {"completed", "cancelled"}:
        if vehicle.attributes.get("active_trip_id") == trip.id:
            if vehicle.state == "in_service":
                _dispatch(
                    engine,
                    vehicle,
                    "release",
                    key=("transit-vehicle", vehicle.id, trip.id, "release"),
                    correlation_id=correlation_id,
                )
                vehicle = _vehicle(persistence, entities)
            vehicle.attributes["active_trip_id"] = None
            with persistence.transaction() as uow:
                uow.save_entity(vehicle)
        return True

    return False
