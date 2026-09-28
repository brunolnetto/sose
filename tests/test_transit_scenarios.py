from sose.backends.simpy import SimPyBackend
from sose.examples.transit.scenarios import ORIGIN, realtime_feed_outage
from sose.examples.transit.simulation import (
    build_runtime,
    record_trip_update,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_realtime_outage_blocks_new_update_without_faking_trip_state():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(
        persistence,
        scenarios=(realtime_feed_outage(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    engine.advance_tick()

    assert record_trip_update(
        persistence,
        engine,
        backend,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=1,
        delay_seconds=120,
    ) is None

    trip = persistence.entity("transit_scheduled_trip", entities.trip_a_id)
    assert trip is not None
    assert trip.state == "planned"
    assert trip.attributes["current_delay_seconds"] == 0

    for _ in range(2):
        engine.advance_tick()

    update = record_trip_update(
        persistence,
        engine,
        backend,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=1,
        delay_seconds=120,
    )
    assert update is not None and update.state == "committed"
