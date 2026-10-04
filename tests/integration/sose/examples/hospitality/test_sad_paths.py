from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.hospitality.scenarios import ORIGIN
from sose.examples.hospitality import simulation as hospitality_simulation
from sose.examples.hospitality.simulation import (
    available_room_ids,
    booking_id,
    build_runtime,
    cancel_reservation,
    check_out,
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


def test_hospitality_private_entity_and_interval_guards():
    persistence, entities, _engine, _backend = _runtime()
    with pytest.raises(RuntimeError, match="was not persisted"):
        hospitality_simulation._entity(persistence, "hospitality_room_booking", "missing")

    with pytest.raises(ValueError, match="departure must be after arrival"):
        available_room_ids(
            persistence,
            entities=entities,
            arrival_at=ORIGIN + timedelta(days=2),
            departure_at=ORIGIN + timedelta(days=2),
        )


def test_create_hold_guard_edges_and_identity_drift():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(1)

    with pytest.raises(ValueError, match="ordinal must be positive"):
        create_hold(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=0,
            arrival_at=arrival,
            departure_at=departure,
        )
    with pytest.raises(ValueError, match="departure must be after arrival"):
        create_hold(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
            arrival_at=arrival,
            departure_at=arrival,
        )
    with pytest.raises(ValueError, match="arrival must be in the future"):
        create_hold(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
            arrival_at=backend.now,
            departure_at=backend.now + timedelta(days=1),
        )
    with pytest.raises(ValueError, match="hold duration must be positive"):
        create_hold(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
            arrival_at=arrival,
            departure_at=departure,
            hold_for=timedelta(0),
        )

    create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=2,
        arrival_at=arrival,
        departure_at=departure,
    )
    with pytest.raises(ValueError, match="identity already exists with different stay"):
        create_hold(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=2,
            arrival_at=arrival + timedelta(hours=1),
            departure_at=departure + timedelta(hours=1),
        )


def test_create_hold_returns_existing_reservation_for_identical_identity():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(2)
    first = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=4,
        arrival_at=arrival,
        departure_at=departure,
    )

    second = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=4,
        arrival_at=arrival,
        departure_at=departure,
    )

    assert second.id == first.id


def test_confirmation_checkout_and_cancellation_guard_edges():
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

    confirmed = confirm_reservation(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    )
    assert confirm_reservation(
        persistence,
        engine,
        reservation_id_value=reservation.id,
    ).id == confirmed.id

    with pytest.raises(RuntimeError, match="check-out requires occupied room booking"):
        check_out(
            persistence,
            engine,
            reservation_id_value=reservation.id,
        )

    check_in(
        persistence,
        engine,
        reservation_id_value=reservation.id,
        at=arrival,
    )
    with pytest.raises(ValueError, match="cannot occur before arrival"):
        check_out(
            persistence,
            engine,
            reservation_id_value=reservation.id,
            at=arrival - timedelta(minutes=1),
        )

    held_reservation = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=2,
        arrival_at=arrival + timedelta(days=2),
        departure_at=departure + timedelta(days=2),
    )
    cancel_reservation(
        persistence,
        engine,
        reservation_id_value=held_reservation.id,
    )
    with pytest.raises(RuntimeError, match="requires held or confirmed reservation"):
        cancel_reservation(
            persistence,
            engine,
            reservation_id_value=held_reservation.id,
        )


def test_confirm_reservation_requires_held_booking_state():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(1)
    reservation = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=6,
        arrival_at=arrival,
        departure_at=departure,
    )
    booking = persistence.entity("hospitality_room_booking", booking_id(reservation.id))
    assert booking is not None
    booking.state = "released"
    with persistence.transaction() as uow:
        uow.save_entity(booking)

    with pytest.raises(RuntimeError, match="active held reservation and booking"):
        confirm_reservation(
            persistence,
            engine,
            reservation_id_value=reservation.id,
        )


def test_record_no_show_occurrence_requires_terminal_no_show_evidence():
    persistence, entities, engine, backend = _runtime()
    arrival, departure = _stay(1)
    reservation = create_hold(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=7,
        arrival_at=arrival,
        departure_at=departure,
    )

    with pytest.raises(RuntimeError, match="terminal no-show evidence"):
        record_no_show_occurrence(
            persistence,
            engine,
            reservation_id_value=reservation.id,
        )
