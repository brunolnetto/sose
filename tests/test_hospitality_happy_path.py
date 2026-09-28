from sose.examples.hospitality.simulation import (
    booking_id,
    reservation_id,
    run_happy_path,
)


def test_confirmed_stay_completes_and_releases_interval():
    persistence, entities = run_happy_path()

    reservation = persistence.entity(
        "hospitality_reservation",
        reservation_id(1),
    )
    booking = persistence.entity(
        "hospitality_room_booking",
        booking_id(reservation_id(1)),
    )

    assert reservation is not None and reservation.state == "checked_out"
    assert booking is not None and booking.state == "completed"
    assert booking.attributes["room_id"] in entities.room_ids
    assert persistence.scheduled_work() == ()
