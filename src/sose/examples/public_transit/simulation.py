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
    StopCall,
    TripUpdate,
    Vehicle,
    VehicleBlock,
    VehiclePositionOccurrence,
)
from .scenarios import ORIGIN
from .statecharts import (
    ScheduledTripChart,
    ServiceAlertChart,
    StopCallChart,
    TripUpdateChart,
    VehicleBlockChart,
    VehicleChart,
    VehiclePositionChart,
)


REALTIME_STALE_AFTER = timedelta(seconds=90)


@dataclass(frozen=True, slots=True)
class TransitEntities:
    vehicle_id: str
    block_id: str
    first_trip_id: str
    second_trip_id: str


def flow_correlation_id(block_id_value: str) -> str:
    return deterministic_id("transit-block-flow", block_id_value)


def vehicle_id() -> str:
    return deterministic_id(
        "entity", "transit_vehicle", "transit-reference", "vehicle-42"
    )


def block_id() -> str:
    return deterministic_id(
        "entity", "transit_vehicle_block", "transit-reference", "block-1"
    )


def trip_id(ordinal: int) -> str:
    return deterministic_id(
        "entity", "transit_scheduled_trip", "transit-reference", f"trip-{ordinal}"
    )


def stop_call_id(trip_id_value: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "transit_stop_call",
        "transit-reference",
        trip_id_value,
        "stop-call",
        sequence,
    )


def trip_update_id(trip_id_value: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "transit_trip_update",
        "transit-reference",
        trip_id_value,
        "trip-update",
        sequence,
    )


def vehicle_position_id(vehicle_id_value: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "transit_vehicle_position",
        "transit-reference",
        vehicle_id_value,
        "position",
        sequence,
    )


def service_alert_id(incident_key: str) -> str:
    return deterministic_id(
        "entity",
        "transit_service_alert",
        "transit-reference",
        "alert",
        incident_key,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(minutes=1), tick=tick),
        random=RandomSource(root_seed=1069),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("transit_vehicle", VehicleChart))
    registry.register(EntityType("transit_vehicle_block", VehicleBlockChart))
    registry.register(EntityType("transit_scheduled_trip", ScheduledTripChart))
    registry.register(EntityType("transit_stop_call", StopCallChart))
    registry.register(EntityType("transit_trip_update", TripUpdateChart))
    registry.register(EntityType("transit_vehicle_position", VehiclePositionChart))
    registry.register(EntityType("transit_service_alert", ServiceAlertChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(persistence: MemoryPersistence) -> TransitEntities:
    context, _ = build_runtime(persistence)
    vehicle = context.entities.create(
        Vehicle,
        key=("transit-reference", "vehicle-42"),
        state="idle",
        attributes={
            "public_vehicle_id": "vehicle-42",
            "current_trip_id": None,
            "latest_position_id": None,
        },
    )
    first = context.entities.create(
        ScheduledTrip,
        key=("transit-reference", "trip-1"),
        state="planned",
        attributes={
            "public_trip_id": "trip-1",
            "block_key": "block-1",
            "ordinal": 1,
            "scheduled_start": ORIGIN.isoformat(),
            "scheduled_end": (ORIGIN + timedelta(minutes=30)).isoformat(),
            "projected_start": ORIGIN.isoformat(),
            "projected_end": (ORIGIN + timedelta(minutes=30)).isoformat(),
            "current_delay_minutes": 0,
        },
    )
    second = context.entities.create(
        ScheduledTrip,
        key=("transit-reference", "trip-2"),
        state="planned",
        attributes={
            "public_trip_id": "trip-2",
            "block_key": "block-1",
            "ordinal": 2,
            "scheduled_start": (ORIGIN + timedelta(minutes=40)).isoformat(),
            "scheduled_end": (ORIGIN + timedelta(minutes=70)).isoformat(),
            "projected_start": (ORIGIN + timedelta(minutes=40)).isoformat(),
            "projected_end": (ORIGIN + timedelta(minutes=70)).isoformat(),
            "current_delay_minutes": 0,
        },
    )
    block = context.entities.create(
        VehicleBlock,
        key=("transit-reference", "block-1"),
        state="planned",
        attributes={
            "public_block_id": "block-1",
            "vehicle_id": vehicle.id,
            "ordered_trip_ids": [first.id, second.id],
        },
    )

    calls = []
    for trip, stop_specs in (
        (
            first,
            (
                (1, "A", ORIGIN),
                (2, "B", ORIGIN + timedelta(minutes=30)),
            ),
        ),
        (
            second,
            (
                (1, "B", ORIGIN + timedelta(minutes=40)),
                (2, "C", ORIGIN + timedelta(minutes=70)),
            ),
        ),
    ):
        for sequence, stop_id_value, scheduled_at in stop_specs:
            calls.append(
                context.entities.create(
                    StopCall,
                    key=(
                        "transit-reference",
                        trip.id,
                        "stop-call",
                        sequence,
                    ),
                    state="pending",
                    attributes={
                        "trip_id": trip.id,
                        "stop_id": stop_id_value,
                        "stop_sequence": sequence,
                        "scheduled_at": scheduled_at.isoformat(),
                        "projected_at": scheduled_at.isoformat(),
                    },
                )
            )

    with persistence.transaction() as uow:
        for entity in (vehicle, first, second, block, *calls):
            uow.save_entity(entity)

    return TransitEntities(
        vehicle_id=vehicle.id,
        block_id=block.id,
        first_trip_id=first.id,
        second_trip_id=second.id,
    )


def _entity(persistence, entity_type: str, entity_id: str):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _dispatch(engine, entity, event: str, *, key, correlation_id: str) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def _block(persistence, entities: TransitEntities) -> VehicleBlock:
    return _entity(persistence, "transit_vehicle_block", entities.block_id)


def _vehicle(persistence, entities: TransitEntities) -> Vehicle:
    return _entity(persistence, "transit_vehicle", entities.vehicle_id)


def _trip(persistence, trip_id_value: str) -> ScheduledTrip:
    return _entity(persistence, "transit_scheduled_trip", trip_id_value)


def _trip_ids(block: VehicleBlock) -> tuple[str, ...]:
    return tuple(str(value) for value in block.attributes["ordered_trip_ids"])


def begin_trip(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TransitEntities,
    trip_id_value: str,
) -> bool:
    block = _block(persistence, entities)
    vehicle = _vehicle(persistence, entities)
    trip = _trip(persistence, trip_id_value)
    ordered = _trip_ids(block)
    if trip.id not in ordered:
        raise ValueError("trip does not belong to vehicle block")
    ordinal = ordered.index(trip.id)
    if ordinal > 0:
        predecessor = _trip(persistence, ordered[ordinal - 1])
        if predecessor.state != "completed":
            raise RuntimeError("downstream block trip requires completed predecessor")

    correlation_id = flow_correlation_id(block.id)
    if block.state == "planned":
        _dispatch(
            engine,
            block,
            "start",
            key=("transit-block", block.id, "start"),
            correlation_id=correlation_id,
        )
    if vehicle.state == "idle":
        _dispatch(
            engine,
            vehicle,
            "assign",
            key=("transit-vehicle", vehicle.id, block.id, "assign"),
            correlation_id=correlation_id,
        )
        vehicle = _vehicle(persistence, entities)
    if vehicle.state != "in_service":
        raise RuntimeError("vehicle is not available for block service")

    if trip.state == "planned":
        _dispatch(
            engine,
            trip,
            "start",
            key=("transit-trip", trip.id, "start"),
            correlation_id=correlation_id,
        )
    elif trip.state != "in_progress":
        return trip.state == "completed"

    vehicle = _vehicle(persistence, entities)
    if vehicle.attributes.get("current_trip_id") not in {None, trip.id}:
        raise RuntimeError("vehicle is already serving another trip")
    vehicle.attributes["current_trip_id"] = trip.id
    with persistence.transaction() as uow:
        uow.save_entity(vehicle)
    return True


def serve_stop_call(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    trip_id_value: str,
    sequence: int,
) -> bool:
    trip = _trip(persistence, trip_id_value)
    if trip.state != "in_progress":
        raise RuntimeError("stop call requires trip in progress")
    call = _entity(
        persistence,
        "transit_stop_call",
        stop_call_id(trip.id, sequence),
    )
    correlation_id = flow_correlation_id(
        str(_entity(
            persistence,
            "transit_vehicle_block",
            block_id(),
        ).id)
    )
    if call.state == "pending":
        _dispatch(
            engine,
            call,
            "arrive",
            key=("transit-stop-call", call.id, "arrive"),
            correlation_id=correlation_id,
        )
        call = _entity(persistence, "transit_stop_call", call.id)
    if call.state == "arrived":
        _dispatch(
            engine,
            call,
            "depart",
            key=("transit-stop-call", call.id, "depart"),
            correlation_id=correlation_id,
        )
    return True


def complete_trip(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TransitEntities,
    trip_id_value: str,
) -> bool:
    block = _block(persistence, entities)
    vehicle = _vehicle(persistence, entities)
    trip = _trip(persistence, trip_id_value)
    if trip.state == "completed":
        return True
    if trip.state != "in_progress":
        return False

    correlation_id = flow_correlation_id(block.id)
    _dispatch(
        engine,
        trip,
        "complete",
        key=("transit-trip", trip.id, "complete"),
        correlation_id=correlation_id,
    )
    ordered = _trip_ids(block)
    vehicle = _vehicle(persistence, entities)
    if vehicle.attributes.get("current_trip_id") == trip.id:
        vehicle.attributes["current_trip_id"] = None
        with persistence.transaction() as uow:
            uow.save_entity(vehicle)

    if trip.id == ordered[-1]:
        block = _block(persistence, entities)
        if block.state == "active":
            _dispatch(
                engine,
                block,
                "complete",
                key=("transit-block", block.id, "complete"),
                correlation_id=correlation_id,
            )
        vehicle = _vehicle(persistence, entities)
        if vehicle.state == "in_service":
            _dispatch(
                engine,
                vehicle,
                "release",
                key=("transit-vehicle", vehicle.id, block.id, "release"),
                correlation_id=correlation_id,
            )
    return True


def _project_trip_delay(
    persistence: MemoryPersistence,
    trip: ScheduledTrip,
    delay_minutes: int,
) -> None:
    scheduled_start = datetime.fromisoformat(str(trip.attributes["scheduled_start"]))
    scheduled_end = datetime.fromisoformat(str(trip.attributes["scheduled_end"]))
    trip.attributes["current_delay_minutes"] = int(delay_minutes)
    trip.attributes["projected_start"] = (
        scheduled_start + timedelta(minutes=delay_minutes)
    ).isoformat()
    trip.attributes["projected_end"] = (
        scheduled_end + timedelta(minutes=delay_minutes)
    ).isoformat()

    calls = []
    for sequence in (1, 2):
        call = _entity(
            persistence,
            "transit_stop_call",
            stop_call_id(trip.id, sequence),
        )
        if call.state == "pending":
            scheduled_at = datetime.fromisoformat(str(call.attributes["scheduled_at"]))
            call.attributes["projected_at"] = (
                scheduled_at + timedelta(minutes=delay_minutes)
            ).isoformat()
            calls.append(call)
    with persistence.transaction() as uow:
        uow.save_entity(trip)
        for call in calls:
            uow.save_entity(call)


def record_trip_delay(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TransitEntities,
    trip_id_value: str,
    sequence: int,
    delay_minutes: int,
) -> TripUpdate:
    if sequence <= 0:
        raise ValueError("sequence must be positive")
    if delay_minutes < 0:
        raise ValueError("delay_minutes must be non-negative")
    block = _block(persistence, entities)
    trip = _trip(persistence, trip_id_value)
    if trip.id not in _trip_ids(block):
        raise ValueError("trip does not belong to vehicle block")
    if trip.state not in {"planned", "in_progress"}:
        raise RuntimeError("delay update requires a planned or in-progress trip")

    uid = trip_update_id(trip.id, sequence)
    update = persistence.entity("transit_trip_update", uid)
    if update is not None:
        if int(update.attributes["delay_minutes"]) != delay_minutes:
            raise ValueError("trip update identity already exists with different delay")
        if update.state == "committed":
            return update
    else:
        update = engine.context.entities.create(
            TripUpdate,
            key=("transit-reference", trip.id, "trip-update", sequence),
            state="captured",
            attributes={
                "trip_id": trip.id,
                "sequence": sequence,
                "delay_minutes": delay_minutes,
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(update)

    _project_trip_delay(persistence, _trip(persistence, trip.id), delay_minutes)

    ordered = _trip_ids(block)
    index = ordered.index(trip.id)
    previous = _trip(persistence, trip.id)
    for downstream_id in ordered[index + 1 :]:
        downstream = _trip(persistence, downstream_id)
        previous_end = datetime.fromisoformat(str(previous.attributes["projected_end"]))
        scheduled_start = datetime.fromisoformat(
            str(downstream.attributes["scheduled_start"])
        )
        propagated = max(
            0,
            int((previous_end - scheduled_start).total_seconds() // 60),
        )
        _project_trip_delay(persistence, downstream, propagated)
        previous = _trip(persistence, downstream.id)

    update = _entity(persistence, "transit_trip_update", update.id)
    if update.state == "captured":
        _dispatch(
            engine,
            update,
            "commit",
            key=("transit-trip-update", update.id, "commit"),
            correlation_id=flow_correlation_id(block.id),
        )
    return _entity(persistence, "transit_trip_update", update.id)


def record_vehicle_position(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TransitEntities,
    trip_id_value: str,
    sequence: int,
    measured_at: datetime,
    latitude: float,
    longitude: float,
) -> VehiclePositionOccurrence | None:
    if sequence <= 0:
        raise ValueError("sequence must be positive")
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        raise ValueError("vehicle position coordinates are invalid")
    if not engine.context.scenarios.attribute("transit.tracking.available", True):
        return None

    trip = _trip(persistence, trip_id_value)
    vehicle = _vehicle(persistence, entities)
    pid = vehicle_position_id(vehicle.id, sequence)
    position = persistence.entity("transit_vehicle_position", pid)
    if position is not None:
        expected = (
            trip.id,
            measured_at.isoformat(),
            float(latitude),
            float(longitude),
        )
        actual = (
            position.attributes["trip_id"],
            position.attributes["measured_at"],
            float(position.attributes["latitude"]),
            float(position.attributes["longitude"]),
        )
        if actual != expected:
            raise ValueError("position identity already exists with different observation")
        if position.state == "committed":
            return position
    else:
        if trip.state != "in_progress":
            raise RuntimeError("new vehicle position requires trip in progress")
        if vehicle.attributes.get("current_trip_id") != trip.id:
            raise RuntimeError("vehicle projection does not match position trip")
        position = engine.context.entities.create(
            VehiclePositionOccurrence,
            key=("transit-reference", vehicle.id, "position", sequence),
            state="captured",
            attributes={
                "vehicle_id": vehicle.id,
                "trip_id": trip.id,
                "sequence": sequence,
                "measured_at": measured_at.isoformat(),
                "latitude": float(latitude),
                "longitude": float(longitude),
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(position)

    position = _entity(persistence, "transit_vehicle_position", position.id)
    if position.state == "captured":
        _dispatch(
            engine,
            position,
            "commit",
            key=("transit-position", position.id, "commit"),
            correlation_id=flow_correlation_id(entities.block_id),
        )
    vehicle = _vehicle(persistence, entities)
    current_measured = vehicle.attributes.get("latest_position_measured_at")
    if current_measured is None or measured_at >= datetime.fromisoformat(
        str(current_measured)
    ):
        vehicle.attributes["latest_position_id"] = position.id
        vehicle.attributes["latest_position_measured_at"] = measured_at.isoformat()
        with persistence.transaction() as uow:
            uow.save_entity(vehicle)
    return _entity(persistence, "transit_vehicle_position", position.id)


def realtime_vehicle_view(
    persistence: MemoryPersistence,
    *,
    entities: TransitEntities,
    as_of: datetime,
) -> dict[str, object]:
    vehicle = _vehicle(persistence, entities)
    position_id_value = vehicle.attributes.get("latest_position_id")
    if position_id_value is None:
        return {
            "vehicle_id": vehicle.attributes["public_vehicle_id"],
            "trip_id": vehicle.attributes.get("current_trip_id"),
            "position": None,
            "stale": True,
        }
    position = _entity(
        persistence,
        "transit_vehicle_position",
        str(position_id_value),
    )
    measured_at = datetime.fromisoformat(str(position.attributes["measured_at"]))
    stale = as_of - measured_at > REALTIME_STALE_AFTER
    return {
        "vehicle_id": vehicle.attributes["public_vehicle_id"],
        "trip_id": vehicle.attributes.get("current_trip_id"),
        "position": None
        if stale
        else {
            "latitude": position.attributes["latitude"],
            "longitude": position.attributes["longitude"],
            "measured_at": position.attributes["measured_at"],
        },
        "stale": stale,
    }


def schedule_service_alert(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: TransitEntities,
    incident_key: str,
    affected_trip_ids: tuple[str, ...],
    start_delay: timedelta,
    duration: timedelta,
) -> tuple[ServiceAlert, datetime, datetime]:
    if not incident_key:
        raise ValueError("incident_key must be non-empty")
    if start_delay < timedelta(0):
        raise ValueError("start_delay must be non-negative")
    if duration <= timedelta(0):
        raise ValueError("duration must be positive")
    block = _block(persistence, entities)
    valid_ids = set(_trip_ids(block))
    affected = tuple(dict.fromkeys(affected_trip_ids))
    if not affected or any(value not in valid_ids for value in affected):
        raise ValueError("alert must target trips in the vehicle block")

    aid = service_alert_id(incident_key)
    alert = persistence.entity("transit_service_alert", aid)
    if alert is None:
        start_at = backend.now + start_delay
        end_at = start_at + duration
        alert = engine.context.entities.create(
            ServiceAlert,
            key=("transit-reference", "alert", incident_key),
            state="scheduled",
            attributes={
                "incident_key": incident_key,
                "affected_trip_ids": list(affected),
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(alert)
    else:
        start_at = datetime.fromisoformat(str(alert.attributes["start_at"]))
        end_at = datetime.fromisoformat(str(alert.attributes["end_at"]))
        if tuple(alert.attributes["affected_trip_ids"]) != affected:
            raise ValueError("alert target scope cannot change on replay")

    correlation_id = flow_correlation_id(block.id)
    if alert.state == "scheduled" and engine.scheduler.find_pending(
        entity_type="transit_service_alert",
        entity_id=alert.id,
        name="activate",
    ) is None:
        command = engine.context.commands.create(
            "activate",
            target=alert,
            due_at=start_at,
            correlation_id=correlation_id,
            key=("transit-alert", alert.id, "activate"),
        )
        engine.context.schedules.at(start_at, command=command)
    if alert.state in {"scheduled", "active"} and engine.scheduler.find_pending(
        entity_type="transit_service_alert",
        entity_id=alert.id,
        name="resolve",
    ) is None:
        command = engine.context.commands.create(
            "resolve",
            target=alert,
            due_at=end_at,
            correlation_id=correlation_id,
            key=("transit-alert", alert.id, "resolve"),
        )
        engine.context.schedules.at(end_at, command=command)
    return _entity(persistence, "transit_service_alert", alert.id), start_at, end_at


def run_happy_path() -> tuple[MemoryPersistence, TransitEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    begin_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
    )
    record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=1,
        measured_at=ORIGIN + timedelta(minutes=5),
        latitude=-16.68,
        longitude=-49.25,
    )
    record_trip_delay(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=1,
        delay_minutes=15,
    )
    complete_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
    )
    begin_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.second_trip_id,
    )
    complete_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.second_trip_id,
    )
    return persistence, entities
