from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import Hotel, NoShowOccurrence, Reservation, Room, RoomBooking
from .scenarios import ORIGIN
from .statecharts import NoShowOccurrenceChart, ReservationChart, RoomBookingChart


DEFAULT_HOLD = timedelta(hours=1)
DEFAULT_NO_SHOW_GRACE = timedelta(hours=2)
BLOCKING_BOOKING_STATES = frozenset({"held", "confirmed", "occupied"})


@dataclass(frozen=True, slots=True)
class HospitalityEntities:
    hotel_id: str
    room_ids: tuple[str, ...]


def flow_correlation_id(reservation_id: str) -> str:
    return deterministic_id("hospitality-flow", reservation_id)


def reservation_id(ordinal: int) -> str:
    return deterministic_id(
        "entity",
        "hospitality_reservation",
        "hospitality-reference",
        "reservation",
        ordinal,
    )


def booking_id(reservation_id_value: str) -> str:
    return deterministic_id(
        "entity",
        "hospitality_room_booking",
        "hospitality-reference",
        reservation_id_value,
        "room-booking",
    )


def no_show_id(reservation_id_value: str) -> str:
    return deterministic_id(
        "entity",
        "hospitality_no_show_occurrence",
        "hospitality-reference",
        reservation_id_value,
        "no-show",
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
        random=RandomSource(root_seed=1319),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("hospitality_reservation", ReservationChart))
    registry.register(EntityType("hospitality_room_booking", RoomBookingChart))
    registry.register(
        EntityType("hospitality_no_show_occurrence", NoShowOccurrenceChart)
    )
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(persistence: MemoryPersistence) -> HospitalityEntities:
    context, _ = build_runtime(persistence)
    room_101 = context.entities.create(
        Room,
        key=("hospitality-reference", "room-101"),
        state="inventory",
        attributes={"room_number": "101", "room_type": "standard"},
    )
    room_102 = context.entities.create(
        Room,
        key=("hospitality-reference", "room-102"),
        state="inventory",
        attributes={"room_number": "102", "room_type": "standard"},
    )
    hotel = context.entities.create(
        Hotel,
        key=("hospitality-reference", "hotel-1"),
        state="open",
        attributes={
            "room_ids": [room_101.id, room_102.id],
            "booking_ids": [],
        },
    )
    with persistence.transaction() as uow:
        for entity in (room_101, room_102, hotel):
            uow.save_entity(entity)
    return HospitalityEntities(
        hotel_id=hotel.id,
        room_ids=(room_101.id, room_102.id),
    )


def _entity(persistence, entity_type: str, entity_id: str):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _hotel(persistence: MemoryPersistence, entities: HospitalityEntities) -> Hotel:
    return _entity(persistence, "hospitality_hotel", entities.hotel_id)


def _reservation(
    persistence: MemoryPersistence,
    reservation_id_value: str,
) -> Reservation:
    return _entity(
        persistence,
        "hospitality_reservation",
        reservation_id_value,
    )


def _booking(
    persistence: MemoryPersistence,
    reservation_id_value: str,
) -> RoomBooking:
    return _entity(
        persistence,
        "hospitality_room_booking",
        booking_id(reservation_id_value),
    )


def _dispatch(engine: Engine, entity, event: str, *, key: tuple[object, ...]) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=flow_correlation_id(
            str(
                entity.attributes.get("reservation_id")
                or entity.id
            )
        ),
        key=key,
    )
    engine.dispatch(command)


def _at(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _overlaps(
    left_start: datetime,
    left_end: datetime,
    right_start: datetime,
    right_end: datetime,
) -> bool:
    return left_start < right_end and right_start < left_end


def available_room_ids(
    persistence: MemoryPersistence,
    *,
    entities: HospitalityEntities,
    arrival_at: datetime,
    departure_at: datetime,
    room_type: str = "standard",
) -> tuple[str, ...]:
    if departure_at <= arrival_at:
        raise ValueError("departure must be after arrival")
    hotel = _hotel(persistence, entities)
    blocked: set[str] = set()
    for bid in hotel.attributes.get("booking_ids", []):
        booking = persistence.entity("hospitality_room_booking", str(bid))
        if booking is None or booking.state not in BLOCKING_BOOKING_STATES:
            continue
        if _overlaps(
            arrival_at,
            departure_at,
            _at(booking.attributes["arrival_at"]),
            _at(booking.attributes["departure_at"]),
        ):
            blocked.add(str(booking.attributes["room_id"]))

    available = []
    for room_id in entities.room_ids:
        room = _entity(persistence, "hospitality_room", room_id)
        if room.attributes["room_type"] == room_type and room.id not in blocked:
            available.append(room.id)
    return tuple(sorted(available))


def create_hold(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: HospitalityEntities,
    ordinal: int,
    arrival_at: datetime,
    departure_at: datetime,
    hold_for: timedelta = DEFAULT_HOLD,
    room_type: str = "standard",
) -> Reservation:
    if ordinal <= 0:
        raise ValueError("reservation ordinal must be positive")
    if departure_at <= arrival_at:
        raise ValueError("departure must be after arrival")
    if arrival_at <= backend.now:
        raise ValueError("arrival must be in the future")
    if hold_for <= timedelta(0):
        raise ValueError("hold duration must be positive")

    rid = reservation_id(ordinal)
    existing = persistence.entity("hospitality_reservation", rid)
    if existing is not None:
        expected = (
            arrival_at.isoformat(),
            departure_at.isoformat(),
            room_type,
        )
        actual = (
            existing.attributes["arrival_at"],
            existing.attributes["departure_at"],
            existing.attributes["room_type"],
        )
        if actual != expected:
            raise ValueError("reservation identity already exists with different stay")
        return existing

    available = available_room_ids(
        persistence,
        entities=entities,
        arrival_at=arrival_at,
        departure_at=departure_at,
        room_type=room_type,
    )
    if not available:
        raise RuntimeError("no room available for requested interval")
    room_id = available[0]
    hold_expires_at = backend.now + hold_for

    reservation = engine.context.entities.create(
        Reservation,
        key=("hospitality-reference", "reservation", ordinal),
        state="requested",
        attributes={
            "ordinal": ordinal,
            "room_id": room_id,
            "room_type": room_type,
            "arrival_at": arrival_at.isoformat(),
            "departure_at": departure_at.isoformat(),
            "hold_expires_at": hold_expires_at.isoformat(),
        },
    )
    booking = engine.context.entities.create(
        RoomBooking,
        key=("hospitality-reference", reservation.id, "room-booking"),
        state="held",
        attributes={
            "reservation_id": reservation.id,
            "room_id": room_id,
            "arrival_at": arrival_at.isoformat(),
            "departure_at": departure_at.isoformat(),
            "hold_expires_at": hold_expires_at.isoformat(),
        },
    )
    hotel = _hotel(persistence, entities)
    booking_ids = list(hotel.attributes.get("booking_ids", []))
    booking_ids.append(booking.id)
    hotel.attributes["booking_ids"] = booking_ids
    with persistence.transaction() as uow:
        uow.save_entity(reservation)
        uow.save_entity(booking)
        uow.save_entity(hotel)

    _dispatch(
        engine,
        reservation,
        "hold",
        key=("hospitality-reservation", reservation.id, "hold"),
    )
    reservation = _reservation(persistence, reservation.id)

    expire_reservation = engine.context.commands.create(
        "expire_hold",
        target=reservation,
        due_at=hold_expires_at,
        correlation_id=flow_correlation_id(reservation.id),
        key=("hospitality-reservation", reservation.id, "expire-hold"),
    )
    expire_booking = engine.context.commands.create(
        "expire",
        target=booking,
        due_at=hold_expires_at,
        correlation_id=flow_correlation_id(reservation.id),
        key=("hospitality-booking", booking.id, "expire"),
    )
    engine.context.schedules.at(hold_expires_at, command=expire_reservation)
    engine.context.schedules.at(hold_expires_at, command=expire_booking)
    return reservation


def confirm_reservation(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    reservation_id_value: str,
    no_show_grace: timedelta = DEFAULT_NO_SHOW_GRACE,
) -> Reservation:
    reservation = _reservation(persistence, reservation_id_value)
    booking = _booking(persistence, reservation.id)
    if reservation.state == "confirmed":
        return reservation
    if reservation.state != "held" or booking.state != "held":
        raise RuntimeError("confirmation requires active held reservation and booking")

    engine.scheduler.cancel_pending(
        entity_type="hospitality_reservation",
        entity_id=reservation.id,
        name="expire_hold",
    )
    engine.scheduler.cancel_pending(
        entity_type="hospitality_room_booking",
        entity_id=booking.id,
        name="expire",
    )
    _dispatch(
        engine,
        reservation,
        "confirm",
        key=("hospitality-reservation", reservation.id, "confirm"),
    )
    _dispatch(
        engine,
        booking,
        "confirm",
        key=("hospitality-booking", booking.id, "confirm"),
    )

    reservation = _reservation(persistence, reservation.id)
    booking = _booking(persistence, reservation.id)
    no_show_at = _at(reservation.attributes["arrival_at"]) + no_show_grace
    reservation.attributes["no_show_at"] = no_show_at.isoformat()
    booking.attributes["no_show_at"] = no_show_at.isoformat()
    with persistence.transaction() as uow:
        uow.save_entity(reservation)
        uow.save_entity(booking)

    reservation_command = engine.context.commands.create(
        "no_show",
        target=reservation,
        due_at=no_show_at,
        correlation_id=flow_correlation_id(reservation.id),
        key=("hospitality-reservation", reservation.id, "no-show"),
    )
    booking_command = engine.context.commands.create(
        "no_show",
        target=booking,
        due_at=no_show_at,
        correlation_id=flow_correlation_id(reservation.id),
        key=("hospitality-booking", booking.id, "no-show"),
    )
    engine.context.schedules.at(no_show_at, command=reservation_command)
    engine.context.schedules.at(no_show_at, command=booking_command)
    return _reservation(persistence, reservation.id)


def check_in(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    reservation_id_value: str,
) -> Reservation:
    reservation = _reservation(persistence, reservation_id_value)
    booking = _booking(persistence, reservation.id)
    if reservation.state != "confirmed" or booking.state != "confirmed":
        raise RuntimeError("check-in requires confirmed reservation and room booking")

    engine.scheduler.cancel_pending(
        entity_type="hospitality_reservation",
        entity_id=reservation.id,
        name="no_show",
    )
    engine.scheduler.cancel_pending(
        entity_type="hospitality_room_booking",
        entity_id=booking.id,
        name="no_show",
    )
    _dispatch(
        engine,
        reservation,
        "check_in",
        key=("hospitality-reservation", reservation.id, "check-in"),
    )
    _dispatch(
        engine,
        booking,
        "occupy",
        key=("hospitality-booking", booking.id, "occupy"),
    )
    return _reservation(persistence, reservation.id)


def check_out(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    reservation_id_value: str,
) -> Reservation:
    reservation = _reservation(persistence, reservation_id_value)
    booking = _booking(persistence, reservation.id)
    if reservation.state != "checked_in" or booking.state != "occupied":
        raise RuntimeError("check-out requires occupied room booking")
    _dispatch(
        engine,
        reservation,
        "check_out",
        key=("hospitality-reservation", reservation.id, "check-out"),
    )
    _dispatch(
        engine,
        booking,
        "complete",
        key=("hospitality-booking", booking.id, "complete"),
    )
    return _reservation(persistence, reservation.id)


def cancel_reservation(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    reservation_id_value: str,
) -> Reservation:
    reservation = _reservation(persistence, reservation_id_value)
    booking = _booking(persistence, reservation.id)
    if reservation.state not in {"held", "confirmed"}:
        raise RuntimeError("cancellation requires held or confirmed reservation")

    for entity_type, entity_id, name in (
        ("hospitality_reservation", reservation.id, "expire_hold"),
        ("hospitality_room_booking", booking.id, "expire"),
        ("hospitality_reservation", reservation.id, "no_show"),
        ("hospitality_room_booking", booking.id, "no_show"),
    ):
        engine.scheduler.cancel_pending(
            entity_type=entity_type,
            entity_id=entity_id,
            name=name,
        )

    _dispatch(
        engine,
        reservation,
        "cancel",
        key=("hospitality-reservation", reservation.id, "cancel"),
    )
    _dispatch(
        engine,
        booking,
        "release",
        key=("hospitality-booking", booking.id, "release"),
    )
    return _reservation(persistence, reservation.id)


def record_no_show_occurrence(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    reservation_id_value: str,
) -> NoShowOccurrence:
    reservation = _reservation(persistence, reservation_id_value)
    booking = _booking(persistence, reservation.id)
    if reservation.state != "no_show_recorded" or booking.state != "no_show_recorded":
        raise RuntimeError("no-show occurrence requires terminal no-show evidence")
    oid = no_show_id(reservation.id)
    existing = persistence.entity("hospitality_no_show_occurrence", oid)
    if existing is not None:
        return existing

    occurrence = engine.context.entities.create(
        NoShowOccurrence,
        key=("hospitality-reference", reservation.id, "no-show"),
        state="captured",
        attributes={
            "reservation_id": reservation.id,
            "booking_id": booking.id,
            "room_id": booking.attributes["room_id"],
            "observed_at": reservation.attributes["no_show_at"],
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(occurrence)
    _dispatch(
        engine,
        occurrence,
        "commit",
        key=("hospitality-no-show", occurrence.id, "commit"),
    )
    return _entity(
        persistence,
        "hospitality_no_show_occurrence",
        occurrence.id,
    )


def run_happy_path() -> tuple[MemoryPersistence, HospitalityEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    reservation = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        arrival_at=ORIGIN + timedelta(days=1),
        departure_at=ORIGIN + timedelta(days=2),
    )
    confirm_reservation(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    )
    check_in(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    )
    check_out(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    )
    return persistence, entities
