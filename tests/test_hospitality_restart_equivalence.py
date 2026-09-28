from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.hospitality.scenarios import ORIGIN
from sose.examples.hospitality.simulation import (
    available_room_ids,
    booking_id,
    build_runtime,
    confirm_reservation,
    create_hold,
    record_no_show_occurrence,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def test_pending_hold_expiry_survives_backend_rebuild():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    arrival = ORIGIN + timedelta(days=1)
    departure = ORIGIN + timedelta(days=2)

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
    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(ORIGIN + timedelta(hours=1))

    booking = persistence.entity(
        "hospitality_room_booking",
        booking_id(reservation.id),
    )
    reservation = persistence.entity("hospitality_reservation", reservation.id)
    assert reservation is not None and reservation.state == "expired"
    assert booking is not None and booking.state == "expired"
    assert len(
        available_room_ids(
            persistence,
            entities=entities,
            arrival_at=arrival,
            departure_at=departure,
        )
    ) == 2


def test_confirmed_no_show_boundary_and_occurrence_survive_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    arrival = ORIGIN + timedelta(days=1)
    departure = ORIGIN + timedelta(days=2)

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
        no_show_grace=timedelta(hours=1),
    )
    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(arrival + timedelta(hours=1))
    occurrence = record_no_show_occurrence(
        persistence,
        rebuilt.engine,
        reservation_id_value=reservation.id,
    )

    booking = persistence.entity(
        "hospitality_room_booking",
        booking_id(reservation.id),
    )
    assert booking is not None and booking.state == "no_show_recorded"
    assert occurrence.state == "committed"
    assert persistence.scheduled_work() == ()
