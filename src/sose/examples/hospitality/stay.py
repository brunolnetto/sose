"""Hospitality check-in/out, cancellation and no-show workflow (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import NoShowOccurrence, Reservation
from .foundation import (
    _at,
    _booking,
    _dispatch,
    _entity,
    _reservation_entity,
    no_show_id,
)


def check_in(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    reservation_id_value: str,
    at: datetime | None = None,
) -> Reservation:
    reservation = _reservation_entity(persistence, reservation_id_value)
    booking = _booking(persistence, reservation.id)
    if reservation.state != "confirmed" or booking.state != "confirmed":
        raise RuntimeError("check-in requires confirmed reservation and room booking")
    at = engine.context.clock.now if at is None else at
    arrival_at = _at(reservation.attributes["arrival_at"])
    departure_at = _at(reservation.attributes["departure_at"])
    if at < arrival_at or at >= departure_at:
        raise ValueError("check-in must occur during the booked stay interval")

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
    return _reservation_entity(persistence, reservation.id)


def check_out(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    reservation_id_value: str,
    at: datetime | None = None,
) -> Reservation:
    reservation = _reservation_entity(persistence, reservation_id_value)
    booking = _booking(persistence, reservation.id)
    if reservation.state != "checked_in" or booking.state != "occupied":
        raise RuntimeError("check-out requires occupied room booking")
    at = engine.context.clock.now if at is None else at
    if at < _at(reservation.attributes["arrival_at"]):
        raise ValueError("check-out cannot occur before arrival")
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
    return _reservation_entity(persistence, reservation.id)


def cancel_reservation(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    reservation_id_value: str,
) -> Reservation:
    reservation = _reservation_entity(persistence, reservation_id_value)
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
    return _reservation_entity(persistence, reservation.id)


def record_no_show_occurrence(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    reservation_id_value: str,
) -> NoShowOccurrence:
    reservation = _reservation_entity(persistence, reservation_id_value)
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
