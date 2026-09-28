from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.transit.scenarios import ORIGIN
from sose.examples.transit.simulation import (
    TRIP_A_START,
    build_runtime,
    ensure_trip_boundaries,
    record_trip_update,
    record_vehicle_position,
    reconcile_vehicle_for_trip,
    schedule_reference_block,
    schedule_service_alert,
    seed_reference,
    trip_update_id,
    vehicle_position_id,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def test_upstream_delay_propagates_only_unabsorbed_delay_to_next_block_trip():
    persistence, entities, engine, _ = _runtime()
    schedule_reference_block(persistence, engine, entities=entities)

    update = record_trip_update(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=1,
        delay_seconds=20 * 60,
    )
    assert update is not None and update.state == "committed"

    trip_a = persistence.entity("transit_scheduled_trip", entities.trip_a_id)
    trip_b = persistence.entity("transit_scheduled_trip", entities.trip_b_id)
    assert trip_a is not None and trip_b is not None
    assert trip_a.attributes["current_delay_seconds"] == 1200
    assert trip_b.attributes["current_delay_seconds"] == 300

    pending_b_start = engine.scheduler.find_pending(
        entity_type="transit_scheduled_trip",
        entity_id=trip_b.id,
        name="start",
    )
    assert pending_b_start is not None
    assert pending_b_start.work.due_at == (
        datetime_from_attr(trip_b.attributes["projected_start_at"])
    )


def datetime_from_attr(value):
    from datetime import datetime

    return datetime.fromisoformat(str(value))


def test_stale_position_is_kept_as_evidence_but_not_current_projection():
    persistence, entities, engine, backend = _runtime()
    ensure_trip_boundaries(
        persistence,
        engine,
        trip_id=entities.trip_a_id,
    )
    backend.run_until(TRIP_A_START)
    assert reconcile_vehicle_for_trip(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
    )

    newer = record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=1,
        observed_at=TRIP_A_START + timedelta(minutes=10),
        stop_sequence=3,
        latitude=-16.68,
        longitude=-49.25,
    )
    stale = record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=2,
        observed_at=TRIP_A_START + timedelta(minutes=5),
        stop_sequence=2,
        latitude=-16.681,
        longitude=-49.251,
    )
    assert newer is not None and stale is not None

    vehicle = persistence.entity("transit_vehicle", entities.vehicle_id)
    assert vehicle is not None
    assert vehicle.attributes["latest_position_id"] == newer.id
    assert vehicle.attributes["current_stop_sequence"] == 3
    assert persistence.entity("transit_vehicle_position", stale.id).state == "committed"


def test_trip_update_identity_is_idempotent_but_conflict_is_rejected():
    persistence, entities, engine, _ = _runtime()
    first = record_trip_update(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=7,
        delay_seconds=60,
    )
    second = record_trip_update(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=7,
        delay_seconds=60,
    )
    assert first is not None and second is not None
    assert first.id == second.id == trip_update_id(entities.trip_a_id, 7)

    with pytest.raises(ValueError, match="different delay"):
        record_trip_update(
            persistence,
            engine,
            entities=entities,
            trip_id=entities.trip_a_id,
            sequence=7,
            delay_seconds=120,
        )


def test_position_requires_running_trip_and_vehicle_ownership():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="running trip"):
        record_vehicle_position(
            persistence,
            engine,
            entities=entities,
            trip_id=entities.trip_a_id,
            sequence=1,
            observed_at=TRIP_A_START,
            stop_sequence=1,
            latitude=-16.68,
            longitude=-49.25,
        )

    assert persistence.entity(
        "transit_vehicle_position",
        vehicle_position_id(entities.vehicle_id, entities.trip_a_id, 1),
    ) is None


def test_service_alert_owns_time_range_and_affected_trip_scope():
    persistence, entities, engine, backend = _runtime()
    start_at = ORIGIN + timedelta(minutes=30)
    end_at = ORIGIN + timedelta(hours=2)
    alert = schedule_service_alert(
        persistence,
        engine,
        alert_key="corridor-disruption",
        affected_trip_ids=(entities.trip_a_id, entities.trip_b_id),
        start_at=start_at,
        end_at=end_at,
    )
    assert alert.attributes["affected_trip_ids"] == [
        entities.trip_a_id,
        entities.trip_b_id,
    ]

    backend.run_until(start_at)
    assert persistence.entity("transit_service_alert", alert.id).state == "active"
    backend.run_until(end_at)
    assert persistence.entity("transit_service_alert", alert.id).state == "cleared"

def test_direct_downstream_delay_is_not_erased_by_upstream_block_update():
    persistence, entities, engine, _ = _runtime()
    schedule_reference_block(persistence, engine, entities=entities)

    direct = record_trip_update(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_b_id,
        sequence=1,
        delay_seconds=10 * 60,
    )
    assert direct is not None
    propagated = record_trip_update(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=1,
        delay_seconds=20 * 60,
    )
    assert propagated is not None

    trip_b = persistence.entity("transit_scheduled_trip", entities.trip_b_id)
    assert trip_b is not None
    assert trip_b.attributes["direct_delay_seconds"] == 600
    assert trip_b.attributes["block_delay_seconds"] == 300
    assert trip_b.attributes["current_delay_seconds"] == 600


def test_service_alert_replay_rejects_conflicting_time_range():
    persistence, entities, engine, _ = _runtime()
    start_at = ORIGIN + timedelta(minutes=30)
    end_at = ORIGIN + timedelta(hours=2)
    first = schedule_service_alert(
        persistence,
        engine,
        alert_key="immutable-window",
        affected_trip_ids=(entities.trip_a_id,),
        start_at=start_at,
        end_at=end_at,
    )

    with pytest.raises(ValueError, match="time range cannot change"):
        schedule_service_alert(
            persistence,
            engine,
            alert_key="immutable-window",
            affected_trip_ids=(entities.trip_a_id,),
            start_at=start_at,
            end_at=end_at + timedelta(minutes=5),
        )

    persisted = persistence.entity("transit_service_alert", first.id)
    assert persisted is not None
    assert persisted.attributes["end_at"] == end_at.isoformat()

