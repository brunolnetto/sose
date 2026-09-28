from datetime import timedelta

import pytest

from sose.examples.public_transit.simulation import (
    ORIGIN,
    begin_trip,
    build_runtime,
    record_trip_delay,
    record_vehicle_position,
    realtime_vehicle_view,
    seed_reference,
    trip_update_id,
    vehicle_position_id,
)
from sose.persistence.memory import MemoryPersistence


def _active_first_trip():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    assert begin_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
    )
    return persistence, entities, engine


def test_position_occurrence_is_idempotent_but_not_rewritable():
    persistence, entities, engine = _active_first_trip()
    measured_at = ORIGIN + timedelta(minutes=1)

    first = record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=7,
        measured_at=measured_at,
        latitude=-16.68,
        longitude=-49.25,
    )
    second = record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=7,
        measured_at=measured_at,
        latitude=-16.68,
        longitude=-49.25,
    )

    assert first is not None and second is not None
    assert first.id == second.id == vehicle_position_id(entities.vehicle_id, 7)
    with pytest.raises(ValueError, match="different observation"):
        record_vehicle_position(
            persistence,
            engine,
            entities=entities,
            trip_id_value=entities.first_trip_id,
            sequence=7,
            measured_at=measured_at,
            latitude=-16.67,
            longitude=-49.25,
        )


def test_out_of_order_position_does_not_move_current_projection_backwards():
    persistence, entities, engine = _active_first_trip()
    newer = ORIGIN + timedelta(minutes=5)
    older = ORIGIN + timedelta(minutes=3)

    latest = record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=2,
        measured_at=newer,
        latitude=-16.68,
        longitude=-49.25,
    )
    record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=1,
        measured_at=older,
        latitude=-16.69,
        longitude=-49.24,
    )
    vehicle = persistence.entity("transit_vehicle", entities.vehicle_id)

    assert latest is not None and vehicle is not None
    assert vehicle.attributes["latest_position_id"] == latest.id


def test_realtime_view_hides_stale_position_without_deleting_occurrence():
    persistence, entities, engine = _active_first_trip()
    measured_at = ORIGIN + timedelta(minutes=1)
    position = record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=1,
        measured_at=measured_at,
        latitude=-16.68,
        longitude=-49.25,
    )
    assert position is not None

    fresh = realtime_vehicle_view(
        persistence,
        entities=entities,
        as_of=measured_at + timedelta(seconds=90),
    )
    stale = realtime_vehicle_view(
        persistence,
        entities=entities,
        as_of=measured_at + timedelta(seconds=91),
    )

    assert fresh["stale"] is False and fresh["position"] is not None
    assert stale["stale"] is True and stale["position"] is None
    assert persistence.entity(
        "transit_vehicle_position",
        vehicle_position_id(entities.vehicle_id, 1),
    ) is not None


def test_trip_update_identity_cannot_change_delay():
    persistence, entities, engine = _active_first_trip()
    first = record_trip_delay(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=9,
        delay_minutes=10,
    )

    assert first.id == trip_update_id(entities.first_trip_id, 9)
    with pytest.raises(ValueError, match="different delay"):
        record_trip_delay(
            persistence,
            engine,
            entities=entities,
            trip_id_value=entities.first_trip_id,
            sequence=9,
            delay_minutes=11,
        )
