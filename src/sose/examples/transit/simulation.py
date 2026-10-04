from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import (
    ScheduledTrip,
    ServiceAlert,
    TripUpdateOccurrence,
    Vehicle,
    VehiclePositionOccurrence,
)
from .scenarios import ORIGIN
from .statecharts import (
    ScheduledTripChart,
    ServiceAlertChart,
    TripUpdateOccurrenceChart,
    VehicleChart,
    VehiclePositionOccurrenceChart,
)


TRIP_A_START = ORIGIN + timedelta(hours=1)
TRIP_A_END = ORIGIN + timedelta(hours=2)
TRIP_B_START = ORIGIN + timedelta(hours=2, minutes=15)
TRIP_B_END = ORIGIN + timedelta(hours=3, minutes=15)
REALTIME_STALE_AFTER = timedelta(seconds=90)


@dataclass(frozen=True, slots=True)
class TransitEntities:
    vehicle_id: str
    trip_a_id: str
    trip_b_id: str


def flow_correlation_id(block_id: str) -> str:
    return deterministic_id("transit-flow", block_id)


def trip_update_id(trip_id: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "transit_trip_update",
        "transit-reference",
        trip_id,
        "trip-update",
        sequence,
    )


def vehicle_position_id(vehicle_id: str, trip_id: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "transit_vehicle_position",
        "transit-reference",
        vehicle_id,
        trip_id,
        "position",
        sequence,
    )


def service_alert_id(alert_key: str) -> str:
    return deterministic_id(
        "entity",
        "transit_service_alert",
        "transit-reference",
        "alert",
        alert_key,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 1117,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("transit_vehicle", VehicleChart))
    registry.register(EntityType("transit_scheduled_trip", ScheduledTripChart))
    registry.register(EntityType("transit_trip_update", TripUpdateOccurrenceChart))
    registry.register(
        EntityType(
            "transit_vehicle_position",
            VehiclePositionOccurrenceChart,
        )
    )
    registry.register(EntityType("transit_service_alert", ServiceAlertChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    vehicle_key: str = "vehicle-1",
    block_id: str = "block-1",
    first_trip_delay: timedelta = timedelta(hours=1),
    trip_duration: timedelta = timedelta(hours=1),
    layover: timedelta = timedelta(minutes=15),
) -> TransitEntities:
    context, _ = build_runtime(persistence, now=now)
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


def _entity(
    persistence: MemoryPersistence,
    entity_type: str,
    entity_id: str,
):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _vehicle(
    persistence: MemoryPersistence,
    entities: TransitEntities,
) -> Vehicle:
    return _entity(persistence, "transit_vehicle", entities.vehicle_id)


def _trip(
    persistence: MemoryPersistence,
    trip_id: str,
) -> ScheduledTrip:
    return _entity(persistence, "transit_scheduled_trip", trip_id)


def _dispatch(
    engine: Engine,
    entity,
    event: str,
    *,
    key: tuple[object, ...],
    correlation_id: str,
) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def _at(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


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


def run_happy_path() -> tuple[MemoryPersistence, TransitEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
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
