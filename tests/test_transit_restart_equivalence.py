from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.transit.entities import VehiclePositionOccurrence
from sose.examples.transit.scenarios import ORIGIN
from sose.examples.transit.simulation import (
    TRIP_A_START,
    build_runtime,
    record_trip_update,
    record_vehicle_position,
    reconcile_vehicle_for_trip,
    schedule_reference_block,
    seed_reference,
    vehicle_position_id,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def _at(value):
    return datetime.fromisoformat(str(value))


def test_delay_projection_and_rescheduled_boundaries_survive_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    schedule_reference_block(persistence, engine, entities=entities)

    record_trip_update(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=1,
        delay_seconds=20 * 60,
    )
    trip_b = persistence.entity("transit_scheduled_trip", entities.trip_b_id)
    assert trip_b is not None
    projected_b_start = _at(trip_b.attributes["projected_start_at"])

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(projected_b_start)

    trip_a = persistence.entity("transit_scheduled_trip", entities.trip_a_id)
    trip_b = persistence.entity("transit_scheduled_trip", entities.trip_b_id)
    assert trip_a is not None and trip_a.state == "completed"
    assert trip_b is not None and trip_b.state == "running"


def test_captured_position_resumes_commit_after_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    schedule_reference_block(persistence, engine, entities=entities)
    backend.run_until(TRIP_A_START)
    assert reconcile_vehicle_for_trip(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
    )

    observed_at = TRIP_A_START + timedelta(minutes=5)
    position = engine.context.entities.create(
        VehiclePositionOccurrence,
        key=(
            "transit-reference",
            entities.vehicle_id,
            entities.trip_a_id,
            "position",
            88,
        ),
        state="captured",
        attributes={
            "vehicle_id": entities.vehicle_id,
            "trip_id": entities.trip_a_id,
            "sequence": 88,
            "observed_at": observed_at.isoformat(),
            "stop_sequence": 2,
            "latitude": -16.68,
            "longitude": -49.25,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(position)

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    recovered = record_vehicle_position(
        persistence,
        rebuilt.engine,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=88,
        observed_at=observed_at,
        stop_sequence=2,
        latitude=-16.68,
        longitude=-49.25,
    )

    assert recovered is not None
    assert recovered.id == vehicle_position_id(
        entities.vehicle_id,
        entities.trip_a_id,
        88,
    )
    assert recovered.state == "committed"
