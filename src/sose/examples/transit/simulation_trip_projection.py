"""Transit trip-update projection helpers (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import (
    ScheduledTrip,
    TripUpdateOccurrence,
)
from .simulation_boundaries import (
    _ensure_boundary,
    _reschedule_trip_boundaries,
)
from .simulation_runtime import (
    _at,
    _dispatch,
    _trip,
    flow_correlation_id,
)

if TYPE_CHECKING:
    from .simulation import TransitEntities


def _apply_trip_projection(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: TransitEntities,
    trip: ScheduledTrip,
    direct_delay_seconds: int | None = None,
    block_delay_seconds: int | None = None,
    latest_update_sequence: int | None = None,
) -> ScheduledTrip:
    projection = _compute_trip_projection(
        trip,
        direct_delay_seconds=direct_delay_seconds,
        block_delay_seconds=block_delay_seconds,
    )
    _persist_trip_projection(
        persistence,
        trip=trip,
        projection=projection,
        latest_update_sequence=latest_update_sequence,
    )

    if projection["changed"]:
        _reconcile_changed_projection(
            persistence,
            engine,
            backend,
            entities=entities,
            trip_id=trip.id,
            trip_state=trip.state,
            projected_start=projection["projected_start"],
            projected_end=projection["projected_end"],
        )
    return _trip(persistence, trip.id)


def _compute_trip_projection(
    trip: ScheduledTrip,
    *,
    direct_delay_seconds: int | None,
    block_delay_seconds: int | None,
) -> dict[str, object]:
    direct = (
        int(trip.attributes.get("direct_delay_seconds", 0))
        if direct_delay_seconds is None
        else int(direct_delay_seconds)
    )
    block = (
        int(trip.attributes.get("block_delay_seconds", 0))
        if block_delay_seconds is None
        else int(block_delay_seconds)
    )
    effective = max(direct, block)
    scheduled_start = _at(trip.attributes["scheduled_start_at"])
    scheduled_end = _at(trip.attributes["scheduled_end_at"])
    projected_start = scheduled_start + timedelta(seconds=effective)
    projected_end = scheduled_end + timedelta(seconds=effective)
    previous_projected_start = _at(trip.attributes["projected_start_at"])
    previous_projected_end = _at(trip.attributes["projected_end_at"])
    changed = (
        previous_projected_start != projected_start
        or previous_projected_end != projected_end
    )
    return {
        "direct": direct,
        "block": block,
        "effective": effective,
        "projected_start": projected_start,
        "projected_end": projected_end,
        "changed": changed,
    }


def _persist_trip_projection(
    persistence: MemoryPersistence,
    *,
    trip: ScheduledTrip,
    projection: dict[str, object],
    latest_update_sequence: int | None,
) -> None:
    trip.attributes["direct_delay_seconds"] = projection["direct"]
    trip.attributes["block_delay_seconds"] = projection["block"]
    trip.attributes["current_delay_seconds"] = projection["effective"]
    trip.attributes["projected_start_at"] = projection["projected_start"].isoformat()
    trip.attributes["projected_end_at"] = projection["projected_end"].isoformat()
    if projection["changed"]:
        trip.attributes["projection_revision"] = (
            int(trip.attributes.get("projection_revision", 0)) + 1
        )
    if latest_update_sequence is not None:
        trip.attributes["latest_update_sequence"] = int(latest_update_sequence)
    with persistence.transaction() as uow:
        uow.save_entity(trip)


def _reconcile_changed_projection(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: TransitEntities,
    trip_id: str,
    trip_state: str,
    projected_start: datetime,
    projected_end: datetime,
) -> None:
    if trip_state == "running" and projected_end <= backend.now:
        _complete_running_trip_from_projection(
            persistence,
            engine,
            trip_id=trip_id,
            projected_end=projected_end,
        )
        return
    if trip_state == "planned" and projected_start <= backend.now:
        _start_planned_trip_from_projection(
            persistence,
            engine,
            trip_id=trip_id,
            projected_start=projected_start,
            projected_end=projected_end,
            backend_now=backend.now,
        )
        return
    _reschedule_trip_boundaries(
        persistence,
        engine,
        backend,
        entities=entities,
        trip_id=trip_id,
    )


def _complete_running_trip_from_projection(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    trip_id: str,
    projected_end: datetime,
) -> None:
    engine.scheduler.cancel_pending(
        entity_type="transit_scheduled_trip",
        entity_id=trip_id,
        name="complete",
    )
    current = _trip(persistence, trip_id)
    _dispatch(
        engine,
        current,
        "complete",
        key=(
            "transit-trip",
            current.id,
            "complete-from-projection",
            projected_end.isoformat(),
        ),
        correlation_id=flow_correlation_id(
            str(current.attributes["block_id"])
        ),
    )


def _start_planned_trip_from_projection(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    trip_id: str,
    projected_start: datetime,
    projected_end: datetime,
    backend_now: datetime,
) -> None:
    for name in ("start", "complete"):
        engine.scheduler.cancel_pending(
            entity_type="transit_scheduled_trip",
            entity_id=trip_id,
            name=name,
        )
    current = _trip(persistence, trip_id)
    _dispatch(
        engine,
        current,
        "start",
        key=(
            "transit-trip",
            current.id,
            "start-from-projection",
            projected_start.isoformat(),
        ),
        correlation_id=flow_correlation_id(
            str(current.attributes["block_id"])
        ),
    )
    current = _trip(persistence, trip_id)
    if projected_end <= backend_now:
        _dispatch(
            engine,
            current,
            "complete",
            key=(
                "transit-trip",
                current.id,
                "complete-from-projection",
                projected_end.isoformat(),
            ),
            correlation_id=flow_correlation_id(
                str(current.attributes["block_id"])
            ),
        )
        return
    _ensure_boundary(
        engine,
        trip=current,
        name="complete",
        due_at=projected_end,
    )


def _reconcile_trip_update_projection(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: TransitEntities,
    occurrence: TripUpdateOccurrence,
) -> None:
    trip = _trip(persistence, str(occurrence.attributes["trip_id"]))
    sequence = int(occurrence.attributes["sequence"])
    delay_seconds = int(occurrence.attributes["delay_seconds"])
    latest_sequence = int(trip.attributes.get("latest_update_sequence", 0))

    if sequence > latest_sequence:
        trip = _apply_trip_projection(
            persistence,
            engine,
            backend,
            entities=entities,
            trip=trip,
            direct_delay_seconds=delay_seconds,
            latest_update_sequence=sequence,
        )

    if trip.id != entities.trip_a_id:
        return

    downstream = _trip(persistence, entities.trip_b_id)
    if downstream.state != "planned":
        return
    upstream_end = _at(trip.attributes["projected_end_at"])
    downstream_scheduled_start = _at(downstream.attributes["scheduled_start_at"])
    propagated_seconds = max(
        0,
        int((upstream_end - downstream_scheduled_start).total_seconds()),
    )
    if int(downstream.attributes.get("block_delay_seconds", 0)) != propagated_seconds:
        _apply_trip_projection(
            persistence,
            engine,
            backend,
            entities=entities,
            trip=downstream,
            block_delay_seconds=propagated_seconds,
        )
