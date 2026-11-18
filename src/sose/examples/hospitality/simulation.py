from __future__ import annotations

from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import Hotel, NoShowOccurrence, Reservation, Room, RoomBooking  # noqa: F401
from .scenarios import ORIGIN
from .statecharts import NoShowOccurrenceChart, ReservationChart, RoomBookingChart  # noqa: F401
from .foundation import (  # noqa: F401
    BLOCKING_BOOKING_STATES,
    DEFAULT_HOLD,
    DEFAULT_NO_SHOW_GRACE,
    HospitalityEntities,
    _at,
    _booking,
    _dispatch,
    _entity,
    _hotel,
    _overlaps,
    _reservation_entity,
    build_runtime,
    flow_correlation_id,
    reservation_id,
    seed_reference,
)
from .stay import (  # noqa: F401
    check_in,
    check_out,
)


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
    reservation = _reservation_entity(persistence, reservation.id)

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
    reservation = _reservation_entity(persistence, reservation_id_value)
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

    reservation = _reservation_entity(persistence, reservation.id)
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
    return _reservation_entity(persistence, reservation.id)


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, HospitalityEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
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
        at=ORIGIN + timedelta(days=1),
    )
    check_out(
        persistence,
        engine,
        reservation_id_value=reservation.id,
        at=ORIGIN + timedelta(days=2),
    )
    return persistence, entities


# Moved by the quality-ratchet splits (#632); importing these names from here
# is deprecated (ADR-0011). Import them from the module named on the right.
from sose._deprecation import deprecated_reexports as _deprecated_reexports

__getattr__ = _deprecated_reexports(__name__, {
    "booking_id": "sose.examples.hospitality.foundation",
    "cancel_reservation": "sose.examples.hospitality.stay",
    "deterministic_id": "sose.core.identity",
    "no_show_id": "sose.examples.hospitality.foundation",
    "record_no_show_occurrence": "sose.examples.hospitality.stay",
}, since="0.8.1", removed_in="0.9.0")
