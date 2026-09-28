from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.hospitality.scenarios import ORIGIN
from sose.examples.hospitality.simulation import (
    available_room_ids,
    booking_id,
    build_runtime,
    cancel_reservation,
    check_in,
    confirm_reservation,
    create_hold,
    record_no_show_occurrence,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _stay(ordinal):
    return (
        ORIGIN + timedelta(days=ordinal),
        ORIGIN + timedelta(days=ordinal + 1),
    )


def test_overlapping_holds_own_distinct_rooms_and_third_is_rejected():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(1)

    first = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        arrival_at=arrival,
        departure_at=departure,
    )
    second = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=2,
        arrival_at=arrival,
        departure_at=departure,
    )

    first_booking = persistence.entity(
        "hospitality_room_booking",
        booking_id(first.id),
    )
    second_booking = persistence.entity(
        "hospitality_room_booking",
        booking_id(second.id),
    )
    assert first_booking is not None and second_booking is not None
    assert first_booking.attributes["room_id"] != second_booking.attributes["room_id"]
    assert available_room_ids(
        persistence,
        entities=entities,
        arrival_at=arrival,
        departure_at=departure,
    ) == ()

    with pytest.raises(RuntimeError, match="no room available"):
        create_hold(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=3,
            arrival_at=arrival,
            departure_at=departure,
        )


def test_hold_expiry_releases_interval_without_deleting_history():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(1)
    reservation = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        arrival_at=arrival,
        departure_at=departure,
        hold_for=timedelta(hours=1),
    )
    booking = persistence.entity(
        "hospitality_room_booking",
        booking_id(reservation.id),
    )
    assert booking is not None
    room_id = booking.attributes["room_id"]

    backend.run_until(ORIGIN + timedelta(hours=1))

    reservation = persistence.entity("hospitality_reservation", reservation.id)
    booking = persistence.entity("hospitality_room_booking", booking.id)
    assert reservation is not None and reservation.state == "expired"
    assert booking is not None and booking.state == "expired"
    assert room_id in available_room_ids(
        persistence,
        entities=entities,
        arrival_at=arrival,
        departure_at=departure,
    )
    assert persistence.scheduled_work() == ()


def test_cancellation_releases_confirmed_future_inventory():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(1)
    reservation = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        arrival_at=arrival,
        departure_at=departure,
    )
    confirm_reservation(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    )
    booking = persistence.entity(
        "hospitality_room_booking",
        booking_id(reservation.id),
    )
    assert booking is not None
    room_id = booking.attributes["room_id"]

    cancel_reservation(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    )

    reservation = persistence.entity("hospitality_reservation", reservation.id)
    booking = persistence.entity("hospitality_room_booking", booking.id)
    assert reservation is not None and reservation.state == "cancelled"
    assert booking is not None and booking.state == "released"
    assert room_id in available_room_ids(
        persistence,
        entities=entities,
        arrival_at=arrival,
        departure_at=departure,
    )
    assert persistence.scheduled_work() == ()


def test_no_show_is_terminal_interval_evidence_and_immutable_occurrence():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(1)
    reservation = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        arrival_at=arrival,
        departure_at=departure,
    )
    reservation = confirm_reservation(
        persistence,
        engine,
        reservation_id_value=reservation.id,
        no_show_grace=timedelta(hours=1),
    )
    no_show_at = arrival + timedelta(hours=1)

    backend.run_until(no_show_at)
    occurrence = record_no_show_occurrence(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    )
    repeated = record_no_show_occurrence(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    )

    reservation = persistence.entity("hospitality_reservation", reservation.id)
    booking = persistence.entity(
        "hospitality_room_booking",
        booking_id(reservation.id),
    )
    assert reservation is not None and reservation.state == "no_show_recorded"
    assert booking is not None and booking.state == "no_show_recorded"
    assert occurrence.state == "committed"
    assert repeated.id == occurrence.id
    assert booking.attributes["room_id"] in available_room_ids(
        persistence,
        entities=entities,
        arrival_at=arrival,
        departure_at=departure,
    )


def test_check_in_requires_confirmed_reservation():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(1)
    reservation = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        arrival_at=arrival,
        departure_at=departure,
    )

    with pytest.raises(RuntimeError, match="confirmed reservation"):
        check_in(
            persistence,
            engine,
            reservation_id_value=reservation.id,
        )


def test_check_in_rejects_time_outside_reserved_interval():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(1)
    reservation = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        arrival_at=arrival,
        departure_at=departure,
    )
    confirm_reservation(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    )

    with pytest.raises(ValueError, match="booked stay interval"):
        check_in(
            persistence,
            engine,
            reservation_id_value=reservation.id,
            at=arrival - timedelta(minutes=1),
        )
