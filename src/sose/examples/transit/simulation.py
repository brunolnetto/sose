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
        sequence,
    )


def vehicle_position_id(vehicle_id: str, trip_id: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "transit_vehicle_position",
        "transit-reference",
        vehicle_id,
        trip_id,
        sequence,
    )


def service_alert_id(alert_key: str) -> str:
    return deterministic_id(
        "entity",
        "transit_service_alert",
        "transit-reference",
        alert_key,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=1117),
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


def seed_reference(persistence: MemoryPersistence) -> TransitEntities:
    context, _ = build_runtime(persistence)
    block_id = "block-1"
    vehicle = context.entities.create(
        Vehicle,
        key=("transit-reference", "vehicle-1"),
        state="available",
        attributes={
            "vehicle_key": "vehicle-1",
            "block_id": block_id,
            "active_trip_id": None,
            "latest_position_id": None,
            "latest_position_observed_at": None,
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
                "current_delay_seconds": 0,
                "latest_update_sequence": 0,
            },
        )

    trip_a = create_trip("trip-a", 1, TRIP_A_START, TRIP_A_END)
    trip_b = create_trip("trip-b", 2, TRIP_B_START, TRIP_B_END)
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
        key=("transit-trip", trip.id, name, due_at.isoformat()),
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


def _reschedule_trip_boundaries(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    trip_id: str,
) -> None:
    trip = _trip(persistence, trip_id)
    for name in ("start", "complete"):
        engine.scheduler.cancel_pending(
            entity_type="transit_scheduled_trip",
            entity_id=trip.id,
            name=name,
        )
    ensure_trip_boundaries(persistence, engine, trip_id=trip.id)


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

    if trip.state == "completed":
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


def record_trip_update(
    persistence: MemoryPersistence,
    engine: Engine,
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

    trip = _trip(persistence, trip.id)
    if sequence <= int(trip.attributes.get("latest_update_sequence", 0)):
        return occurrence

    delay = timedelta(seconds=delay_seconds)
    scheduled_start = _at(trip.attributes["scheduled_start_at"])
    scheduled_end = _at(trip.attributes["scheduled_end_at"])
    trip.attributes["projected_start_at"] = (scheduled_start + delay).isoformat()
    trip.attributes["projected_end_at"] = (scheduled_end + delay).isoformat()
    trip.attributes["current_delay_seconds"] = int(delay_seconds)
    trip.attributes["latest_update_sequence"] = sequence
    with persistence.transaction() as uow:
        uow.save_entity(trip)
    _reschedule_trip_boundaries(
        persistence,
        engine,
        trip_id=trip.id,
    )

    if trip.id == entities.trip_a_id:
        downstream = _trip(persistence, entities.trip_b_id)
        if downstream.state == "planned":
            upstream_end = _at(trip.attributes["projected_end_at"])
            downstream_scheduled_start = _at(
                downstream.attributes["scheduled_start_at"]
            )
            propagated_seconds = max(
                0,
                int(
                    (upstream_end - downstream_scheduled_start).total_seconds()
                ),
            )
            downstream_start = downstream_scheduled_start + timedelta(
                seconds=propagated_seconds
            )
            downstream_end = _at(
                downstream.attributes["scheduled_end_at"]
            ) + timedelta(seconds=propagated_seconds)
            downstream.attributes["projected_start_at"] = (
                downstream_start.isoformat()
            )
            downstream.attributes["projected_end_at"] = downstream_end.isoformat()
            downstream.attributes["current_delay_seconds"] = propagated_seconds
            with persistence.transaction() as uow:
                uow.save_entity(downstream)
            _reschedule_trip_boundaries(
                persistence,
                engine,
                trip_id=downstream.id,
            )

    return occurrence


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
    if sequence <= 0:
        raise ValueError("vehicle position sequence must be positive")
    if stop_sequence <= 0:
        raise ValueError("stop_sequence must be positive")
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        raise ValueError("vehicle position coordinates are invalid")

    trip = _trip(persistence, trip_id)
    vehicle = _vehicle(persistence, entities)
    pid = vehicle_position_id(vehicle.id, trip.id, sequence)
    existing = persistence.entity("transit_vehicle_position", pid)
    if existing is not None:
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
        return existing

    if not engine.context.scenarios.attribute("transit.realtime.available", True):
        return None
    if trip.state != "running":
        raise RuntimeError("vehicle position requires running trip")
    if (
        vehicle.state != "in_service"
        or vehicle.attributes.get("active_trip_id") != trip.id
    ):
        raise RuntimeError("vehicle position requires vehicle assigned to trip")

    position = engine.context.entities.create(
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

    vehicle = _vehicle(persistence, entities)
    latest_observed_at = vehicle.attributes.get("latest_position_observed_at")
    if (
        latest_observed_at is None
        or observed_at > datetime.fromisoformat(str(latest_observed_at))
    ):
        vehicle.attributes["latest_position_id"] = position.id
        vehicle.attributes["latest_position_observed_at"] = observed_at.isoformat()
        vehicle.attributes["current_stop_sequence"] = stop_sequence
        with persistence.transaction() as uow:
            uow.save_entity(vehicle)
    return position


def schedule_service_alert(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    alert_key: str,
    affected_trip_ids: tuple[str, ...],
    start_at: datetime,
    end_at: datetime,
) -> ServiceAlert:
    if not alert_key:
        raise ValueError("alert_key must be non-empty")
    if not affected_trip_ids:
        raise ValueError("service alert requires affected trips")
    if end_at <= start_at:
        raise ValueError("service alert end must be after start")
    for trip_id in affected_trip_ids:
        _trip(persistence, trip_id)

    aid = service_alert_id(alert_key)
    alert = persistence.entity("transit_service_alert", aid)
    requested = tuple(dict.fromkeys(affected_trip_ids))
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
    else:
        if tuple(alert.attributes["affected_trip_ids"]) != requested:
            raise ValueError("service alert scope cannot change on replay")
        start_at = _at(alert.attributes["start_at"])
        end_at = _at(alert.attributes["end_at"])

    if alert.state == "scheduled" and engine.scheduler.find_pending(
        entity_type="transit_service_alert",
        entity_id=alert.id,
        name="activate",
    ) is None:
        command = engine.context.commands.create(
            "activate",
            target=alert,
            due_at=start_at,
            correlation_id=deterministic_id("transit-alert", alert.id),
            key=("transit-alert", alert.id, "activate"),
        )
        engine.context.schedules.at(start_at, command=command)

    if alert.state in {"scheduled", "active"} and engine.scheduler.find_pending(
        entity_type="transit_service_alert",
        entity_id=alert.id,
        name="clear",
    ) is None:
        command = engine.context.commands.create(
            "clear",
            target=alert,
            due_at=end_at,
            correlation_id=deterministic_id("transit-alert", alert.id),
            key=("transit-alert", alert.id, "clear"),
        )
        engine.context.schedules.at(end_at, command=command)

    return _entity(persistence, "transit_service_alert", alert.id)


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
