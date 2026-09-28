from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.public_transit.simulation import (
    ORIGIN,
    begin_trip,
    build_runtime,
    record_trip_delay,
    record_vehicle_position,
    schedule_service_alert,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_downstream_trip_cannot_start_before_predecessor_completes():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    with pytest.raises(RuntimeError, match="completed predecessor"):
        begin_trip(
            persistence,
            engine,
            entities=entities,
            trip_id_value=entities.second_trip_id,
        )


def test_negative_delay_is_rejected():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    with pytest.raises(ValueError, match="non-negative"):
        record_trip_delay(
            persistence,
            engine,
            entities=entities,
            trip_id_value=entities.first_trip_id,
            sequence=1,
            delay_minutes=-1,
        )


def test_invalid_vehicle_coordinates_are_rejected():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    begin_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
    )

    with pytest.raises(ValueError, match="coordinates"):
        record_vehicle_position(
            persistence,
            engine,
            entities=entities,
            trip_id_value=entities.first_trip_id,
            sequence=1,
            measured_at=ORIGIN,
            latitude=91.0,
            longitude=-49.25,
        )


def test_alert_requires_positive_duration_and_known_trip_scope():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    with pytest.raises(ValueError, match="duration"):
        schedule_service_alert(
            persistence,
            engine,
            backend,
            entities=entities,
            incident_key="closure",
            affected_trip_ids=(entities.first_trip_id,),
            start_delay=timedelta(0),
            duration=timedelta(0),
        )

    with pytest.raises(ValueError, match="vehicle block"):
        schedule_service_alert(
            persistence,
            engine,
            backend,
            entities=entities,
            incident_key="closure",
            affected_trip_ids=("not-a-trip",),
            start_delay=timedelta(0),
            duration=timedelta(minutes=5),
        )
