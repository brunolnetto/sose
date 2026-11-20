from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import ScheduledTrip, TripUpdateOccurrence, Vehicle
from .scenarios import ORIGIN
from .simulation_runtime import (
    TRIP_A_START,
    flow_correlation_id,
    trip_update_id,
    build_runtime,
    _entity,
    _trip,
    _dispatch,
    _at,
)
from .simulation_boundaries import schedule_reference_block, reconcile_vehicle_for_trip
from .simulation_trip_projection import _reconcile_trip_update_projection
from .simulation_vehicle_positions import record_vehicle_position


@dataclass(frozen=True, slots=True)
class TransitEntities:
    vehicle_id: str
    trip_a_id: str
    trip_b_id: str


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    vehicle_key: str = "vehicle-1",
    block_id: str = "block-1",
    first_trip_delay: timedelta = timedelta(hours=1),
    trip_duration: timedelta = timedelta(hours=1),
    layover: timedelta = timedelta(minutes=15),
) -> TransitEntities:
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
    first_start = now + first_trip_delay
    first_end = first_start + trip_duration
    second_start = first_end + layover
    second_end = second_start + trip_duration
    vehicle = context.entities.create(
        Vehicle,
        key=("transit-reference", vehicle_key),
        state="available",
        attributes={
            "vehicle_key": vehicle_key,
            "block_id": block_id,
            "active_trip_id": None,
            "latest_position_id": None,
            "latest_position_observed_at": None,
            "latest_position_sequence": 0,
            "current_stop_sequence": None,
        },
    )

    def create_trip(
        trip_key: str,
        ordinal: int,
        start_at: datetime,
        end_at: datetime,
    ) -> ScheduledTrip:
        return context.entities.create(
            ScheduledTrip,
            key=("transit-reference", trip_key),
            state="planned",
            attributes={
                "trip_key": trip_key,
                "block_id": block_id,
                "block_ordinal": ordinal,
                "vehicle_id": vehicle.id,
                "scheduled_start_at": start_at.isoformat(),
                "scheduled_end_at": end_at.isoformat(),
                "projected_start_at": start_at.isoformat(),
                "projected_end_at": end_at.isoformat(),
                "direct_delay_seconds": 0,
                "block_delay_seconds": 0,
                "current_delay_seconds": 0,
                "latest_update_sequence": 0,
                "projection_revision": 0,
            },
        )

    trip_a = create_trip("trip-a", 1, first_start, first_end)
    trip_b = create_trip("trip-b", 2, second_start, second_end)
    with persistence.transaction() as uow:
        for entity in (vehicle, trip_a, trip_b):
            uow.save_entity(entity)

    return TransitEntities(
        vehicle_id=vehicle.id,
        trip_a_id=trip_a.id,
        trip_b_id=trip_b.id,
    )


def record_trip_update(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: TransitEntities,
    trip_id: str,
    sequence: int,
    delay_seconds: int,
) -> TripUpdateOccurrence | None:
    if sequence <= 0:
        raise ValueError("trip update sequence must be positive")
    if delay_seconds < 0:
        raise ValueError("reference trip delay must be non-negative")

    trip = _trip(persistence, trip_id)
    uid = trip_update_id(trip.id, sequence)
    existing = persistence.entity("transit_trip_update", uid)
    if existing is not None:
        if int(existing.attributes["delay_seconds"]) != int(delay_seconds):
            raise ValueError(
                "trip update identity already exists with different delay"
            )
        if existing.state == "captured":
            _dispatch(
                engine,
                existing,
                "commit",
                key=("transit-update", existing.id, "commit"),
                correlation_id=flow_correlation_id(
                    str(trip.attributes["block_id"])
                ),
            )
            existing = _entity(
                persistence,
                "transit_trip_update",
                existing.id,
            )
        _reconcile_trip_update_projection(
            persistence,
            engine,
            backend,
            entities=entities,
            occurrence=existing,
        )
        projected_trip = _trip(persistence, trip.id)
        if projected_trip.state == "completed":
            reconcile_vehicle_for_trip(
                persistence,
                engine,
                entities=entities,
                trip_id=projected_trip.id,
            )
        return existing

    if not engine.context.scenarios.attribute("transit.realtime.available", True):
        return None
    if trip.state not in {"planned", "running"}:
        raise RuntimeError(
            f"realtime update requires planned/running trip, got {trip.state}"
        )

    occurrence = engine.context.entities.create(
        TripUpdateOccurrence,
        key=("transit-reference", trip.id, "trip-update", sequence),
        state="captured",
        attributes={
            "trip_id": trip.id,
            "sequence": sequence,
            "delay_seconds": int(delay_seconds),
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(occurrence)
    _dispatch(
        engine,
        occurrence,
        "commit",
        key=("transit-update", occurrence.id, "commit"),
        correlation_id=flow_correlation_id(str(trip.attributes["block_id"])),
    )
    occurrence = _entity(persistence, "transit_trip_update", occurrence.id)
    _reconcile_trip_update_projection(
        persistence,
        engine,
        backend,
        entities=entities,
        occurrence=occurrence,
    )
    projected_trip = _trip(persistence, trip.id)
    if projected_trip.state == "completed":
        reconcile_vehicle_for_trip(
            persistence,
            engine,
            entities=entities,
            trip_id=projected_trip.id,
        )
    return occurrence


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, TransitEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    schedule_reference_block(persistence, engine, entities=entities)

    trip_a = _trip(persistence, entities.trip_a_id)
    backend.run_until(_at(trip_a.attributes["projected_start_at"]))
    reconcile_vehicle_for_trip(
        persistence,
        engine,
        entities=entities,
        trip_id=trip_a.id,
    )
    record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id=trip_a.id,
        sequence=1,
        observed_at=TRIP_A_START + timedelta(minutes=10),
        stop_sequence=2,
        latitude=-16.6799,
        longitude=-49.2550,
    )

    backend.run_until(_at(trip_a.attributes["projected_end_at"]))
    reconcile_vehicle_for_trip(
        persistence,
        engine,
        entities=entities,
        trip_id=trip_a.id,
    )

    trip_b = _trip(persistence, entities.trip_b_id)
    backend.run_until(_at(trip_b.attributes["projected_start_at"]))
    reconcile_vehicle_for_trip(
        persistence,
        engine,
        entities=entities,
        trip_id=trip_b.id,
    )
    backend.run_until(_at(trip_b.attributes["projected_end_at"]))
    reconcile_vehicle_for_trip(
        persistence,
        engine,
        entities=entities,
        trip_id=trip_b.id,
    )
    return persistence, entities
