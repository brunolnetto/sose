"""Transit vehicle positions and realtime vehicle view (split from simulation.py)."""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import (
    ScheduledTrip,
    VehiclePositionOccurrence,
)
from .simulation_runtime import (
    REALTIME_STALE_AFTER,
    _at,
    _dispatch,
    _entity,
    _trip,
    _vehicle,
    flow_correlation_id,
    vehicle_position_id,
)

if TYPE_CHECKING:
    from .simulation import TransitEntities


def _reconcile_vehicle_position_projection(
    persistence: MemoryPersistence,
    *,
    entities: TransitEntities,
    position: VehiclePositionOccurrence,
) -> None:
    vehicle = _vehicle(persistence, entities)
    observed_at = _at(position.attributes["observed_at"])
    latest_observed_at = vehicle.attributes.get("latest_position_observed_at")
    if latest_observed_at is not None:
        latest_at = _at(latest_observed_at)
        latest_id = vehicle.attributes.get("latest_position_id")
        latest = (
            None
            if latest_id is None
            else persistence.entity("transit_vehicle_position", str(latest_id))
        )
        latest_sequence = -1 if latest is None else int(latest.attributes["sequence"])
        candidate_key = (observed_at, int(position.attributes["sequence"]), position.id)
        latest_key = (
            latest_at,
            latest_sequence,
            "" if latest is None else latest.id,
        )
        if candidate_key <= latest_key:
            return
    vehicle.attributes["latest_position_id"] = position.id
    vehicle.attributes["latest_position_observed_at"] = observed_at.isoformat()
    vehicle.attributes["latest_position_sequence"] = int(position.attributes["sequence"])
    vehicle.attributes["current_stop_sequence"] = int(
        position.attributes["stop_sequence"]
    )
    with persistence.transaction() as uow:
        uow.save_entity(vehicle)


def record_vehicle_position(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TransitEntities,
    trip_id: str,
    sequence: int,
    observed_at: datetime,
    stop_sequence: int,
    latitude: float,
    longitude: float,
) -> VehiclePositionOccurrence | None:
    _validate_vehicle_position_inputs(
        sequence=sequence,
        stop_sequence=stop_sequence,
        latitude=latitude,
        longitude=longitude,
    )

    trip = _trip(persistence, trip_id)
    vehicle = _vehicle(persistence, entities)
    pid = vehicle_position_id(vehicle.id, trip.id, sequence)
    existing = persistence.entity("transit_vehicle_position", pid)
    if existing is not None:
        return _reconcile_existing_vehicle_position(
            persistence,
            engine,
            entities=entities,
            trip=trip,
            existing=existing,
            observed_at=observed_at,
            stop_sequence=stop_sequence,
            latitude=latitude,
            longitude=longitude,
        )

    if not engine.context.scenarios.attribute("transit.realtime.available", True):
        return None
    _assert_vehicle_position_preconditions(
        trip=trip,
        vehicle=vehicle,
    )
    position = _create_vehicle_position(
        engine,
        vehicle=vehicle,
        trip=trip,
        sequence=sequence,
        observed_at=observed_at,
        stop_sequence=stop_sequence,
        latitude=latitude,
        longitude=longitude,
    )
    with persistence.transaction() as uow:
        uow.save_entity(position)
    _dispatch(
        engine,
        position,
        "commit",
        key=("transit-position", position.id, "commit"),
        correlation_id=flow_correlation_id(str(trip.attributes["block_id"])),
    )
    position = _entity(
        persistence,
        "transit_vehicle_position",
        position.id,
    )
    _reconcile_vehicle_position_projection(
        persistence,
        entities=entities,
        position=position,
    )
    return position


def _validate_vehicle_position_inputs(
    *,
    sequence: int,
    stop_sequence: int,
    latitude: float,
    longitude: float,
) -> None:
    if sequence <= 0:
        raise ValueError("vehicle position sequence must be positive")
    if stop_sequence <= 0:
        raise ValueError("stop_sequence must be positive")
    if not _coordinates_valid(latitude=latitude, longitude=longitude):
        raise ValueError("vehicle position coordinates are invalid")


def _coordinates_valid(*, latitude: float, longitude: float) -> bool:
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return False
    return -90 <= latitude <= 90 and -180 <= longitude <= 180


def _assert_vehicle_position_preconditions(*, trip: ScheduledTrip, vehicle) -> None:
    if trip.state != "running":
        raise RuntimeError("vehicle position requires running trip")
    if vehicle.state != "in_service":
        raise RuntimeError("vehicle position requires vehicle assigned to trip")
    if vehicle.attributes.get("active_trip_id") != trip.id:
        raise RuntimeError("vehicle position requires vehicle assigned to trip")


def _create_vehicle_position(
    engine: Engine,
    *,
    vehicle,
    trip: ScheduledTrip,
    sequence: int,
    observed_at: datetime,
    stop_sequence: int,
    latitude: float,
    longitude: float,
) -> VehiclePositionOccurrence:
    return engine.context.entities.create(
        VehiclePositionOccurrence,
        key=(
            "transit-reference",
            vehicle.id,
            trip.id,
            "position",
            sequence,
        ),
        state="captured",
        attributes={
            "vehicle_id": vehicle.id,
            "trip_id": trip.id,
            "sequence": sequence,
            "observed_at": observed_at.isoformat(),
            "stop_sequence": stop_sequence,
            "latitude": float(latitude),
            "longitude": float(longitude),
        },
    )


def _reconcile_existing_vehicle_position(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TransitEntities,
    trip: ScheduledTrip,
    existing: VehiclePositionOccurrence,
    observed_at: datetime,
    stop_sequence: int,
    latitude: float,
    longitude: float,
) -> VehiclePositionOccurrence:
    if (
        existing.attributes["observed_at"] != observed_at.isoformat()
        or int(existing.attributes["stop_sequence"]) != stop_sequence
        or float(existing.attributes["latitude"]) != float(latitude)
        or float(existing.attributes["longitude"]) != float(longitude)
    ):
        raise ValueError(
            "vehicle position identity already exists with different observation"
        )
    if existing.state == "captured":
        _dispatch(
            engine,
            existing,
            "commit",
            key=("transit-position", existing.id, "commit"),
            correlation_id=flow_correlation_id(
                str(trip.attributes["block_id"])
            ),
        )
        existing = _entity(
            persistence,
            "transit_vehicle_position",
            existing.id,
        )
    _reconcile_vehicle_position_projection(
        persistence,
        entities=entities,
        position=existing,
    )
    return existing


def realtime_vehicle_view(
    persistence: MemoryPersistence,
    *,
    entities: TransitEntities,
    as_of: datetime,
) -> dict[str, object]:
    """Project only fresh realtime position while retaining durable history."""
    vehicle = _vehicle(persistence, entities)
    latest_id = vehicle.attributes.get("latest_position_id")
    if latest_id is None:
        return {
            "vehicle_id": vehicle.id,
            "active_trip_id": vehicle.attributes.get("active_trip_id"),
            "position": None,
            "stale": True,
        }

    position = _entity(
        persistence,
        "transit_vehicle_position",
        str(latest_id),
    )
    observed_at = _at(position.attributes["observed_at"])
    stale = as_of - observed_at > REALTIME_STALE_AFTER
    return {
        "vehicle_id": vehicle.id,
        "active_trip_id": vehicle.attributes.get("active_trip_id"),
        "position": None if stale else {
            "latitude": position.attributes["latitude"],
            "longitude": position.attributes["longitude"],
            "observed_at": position.attributes["observed_at"],
            "stop_sequence": position.attributes["stop_sequence"],
        },
        "stale": stale,
    }
