from sose.examples.transit.simulation import (
    run_happy_path,
    vehicle_position_id,
)


def test_two_trip_block_runs_with_one_vehicle_and_position_evidence():
    persistence, entities = run_happy_path()

    vehicle = persistence.entity("transit_vehicle", entities.vehicle_id)
    trip_a = persistence.entity("transit_scheduled_trip", entities.trip_a_id)
    trip_b = persistence.entity("transit_scheduled_trip", entities.trip_b_id)

    assert vehicle is not None and vehicle.state == "available"
    assert vehicle.attributes["active_trip_id"] is None
    assert trip_a is not None and trip_a.state == "completed"
    assert trip_b is not None and trip_b.state == "completed"

    position = persistence.entity(
        "transit_vehicle_position",
        vehicle_position_id(vehicle.id, trip_a.id, 1),
    )
    assert position is not None and position.state == "committed"
    assert vehicle.attributes["latest_position_id"] == position.id
    assert persistence.scheduled_work() == ()
