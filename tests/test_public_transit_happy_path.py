from datetime import datetime

from sose.examples.public_transit.simulation import (
    begin_trip,
    build_runtime,
    complete_trip,
    record_trip_delay,
    run_happy_path,
    seed_reference,
    stop_call_id,
)
from sose.persistence.memory import MemoryPersistence


def test_block_delay_consumes_layover_before_propagating_downstream():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    assert begin_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
    )
    update = record_trip_delay(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=1,
        delay_minutes=15,
    )

    first = persistence.entity("transit_scheduled_trip", entities.first_trip_id)
    second = persistence.entity("transit_scheduled_trip", entities.second_trip_id)
    second_first_call = persistence.entity(
        "transit_stop_call",
        stop_call_id(entities.second_trip_id, 1),
    )

    assert update.state == "committed"
    assert first is not None and first.attributes["current_delay_minutes"] == 15
    assert second is not None and second.attributes["current_delay_minutes"] == 5
    assert datetime.fromisoformat(second.attributes["projected_start"]).minute == 45
    assert second_first_call is not None
    assert datetime.fromisoformat(second_first_call.attributes["projected_at"]).minute == 45


def test_one_physical_vehicle_serves_two_ordered_trips_then_releases():
    persistence, entities = run_happy_path()

    vehicle = persistence.entity("transit_vehicle", entities.vehicle_id)
    block = persistence.entity("transit_vehicle_block", entities.block_id)
    first = persistence.entity("transit_scheduled_trip", entities.first_trip_id)
    second = persistence.entity("transit_scheduled_trip", entities.second_trip_id)

    assert vehicle is not None and vehicle.state == "idle"
    assert vehicle.attributes["current_trip_id"] is None
    assert block is not None and block.state == "completed"
    assert first is not None and first.state == "completed"
    assert second is not None and second.state == "completed"


def test_second_trip_starts_after_predecessor_completion():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    assert begin_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
    )
    assert complete_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
    )
    assert begin_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.second_trip_id,
    )
