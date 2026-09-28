from datetime import timedelta

from sose.examples.public_transit.scenarios import ORIGIN, vehicle_tracking_outage
from sose.examples.public_transit.simulation import (
    begin_trip,
    build_runtime,
    record_vehicle_position,
    seed_reference,
    vehicle_position_id,
)
from sose.persistence.memory import MemoryPersistence


def test_tracking_outage_does_not_fabricate_observations_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=vehicle_tracking_outage(),
    )
    begin_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
    )

    engine.advance_tick()
    assert record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=1,
        measured_at=context.clock.now,
        latitude=-16.68,
        longitude=-49.25,
    ) is None
    assert persistence.entity(
        "transit_vehicle_position",
        vehicle_position_id(entities.vehicle_id, 1),
    ) is None

    for _ in range(2):
        engine.advance_tick()

    recovered = record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=2,
        measured_at=ORIGIN + timedelta(minutes=3),
        latitude=-16.68,
        longitude=-49.25,
    )
    assert recovered is not None and recovered.state == "committed"
