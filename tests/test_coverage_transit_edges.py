from __future__ import annotations

from datetime import timedelta
import math

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.transit.scenarios import ORIGIN
from sose.examples.transit.simulation import (
    TRIP_A_START,
    _entity,
    build_runtime,
    cancel_service_alert,
    cancel_trip,
    ensure_trip_boundaries,
    record_trip_update,
    record_vehicle_position,
    realtime_vehicle_view,
    reconcile_vehicle_for_trip,
    schedule_service_alert,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def _start_trip_a(persistence, entities, engine, backend, *, assign=True):
    ensure_trip_boundaries(persistence, engine, trip_id=entities.trip_a_id)
    backend.run_until(TRIP_A_START)
    trip = persistence.entity("transit_scheduled_trip", entities.trip_a_id)
    assert trip is not None and trip.state == "running"
    if assign:
        assert reconcile_vehicle_for_trip(
            persistence,
            engine,
            entities=entities,
            trip_id=entities.trip_a_id,
        )


def test_missing_transit_entity_guard_is_observable():
    persistence = MemoryPersistence()

    with pytest.raises(RuntimeError, match="was not persisted"):
        _entity(persistence, "transit_vehicle", "missing")


def test_running_trip_rejects_vehicle_owned_by_another_trip():
    persistence, entities, engine, backend = _runtime()
    _start_trip_a(persistence, entities, engine, backend, assign=False)

    vehicle = persistence.entity("transit_vehicle", entities.vehicle_id)
    assert vehicle is not None
    vehicle.attributes["active_trip_id"] = entities.trip_b_id
    _save(persistence, vehicle)

    with pytest.raises(RuntimeError, match="already serves another trip"):
        reconcile_vehicle_for_trip(
            persistence,
            engine,
            entities=entities,
            trip_id=entities.trip_a_id,
        )


def test_running_trip_requires_vehicle_in_service_when_not_assignable():
    persistence, entities, engine, backend = _runtime()
    _start_trip_a(persistence, entities, engine, backend, assign=False)

    vehicle = persistence.entity("transit_vehicle", entities.vehicle_id)
    assert vehicle is not None
    vehicle.state = "maintenance"
    _save(persistence, vehicle)

    with pytest.raises(RuntimeError, match="requires vehicle in_service"):
        reconcile_vehicle_for_trip(
            persistence,
            engine,
            entities=entities,
            trip_id=entities.trip_a_id,
        )


def test_vehicle_reconcile_returns_false_for_planned_trip():
    persistence, entities, engine, _ = _runtime()

    assert (
        reconcile_vehicle_for_trip(
            persistence,
            engine,
            entities=entities,
            trip_id=entities.trip_a_id,
        )
        is False
    )


@pytest.mark.parametrize(
    ("sequence", "delay", "message"),
    [
        (0, 0, "sequence must be positive"),
        (1, -1, "delay must be non-negative"),
    ],
)
def test_trip_update_validates_sequence_and_delay(sequence, delay, message):
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(ValueError, match=message):
        record_trip_update(
            persistence,
            engine,
            backend,
            entities=entities,
            trip_id=entities.trip_a_id,
            sequence=sequence,
            delay_seconds=delay,
        )


def test_trip_update_rejects_terminal_trip():
    persistence, entities, engine, backend = _runtime()
    trip = persistence.entity("transit_scheduled_trip", entities.trip_a_id)
    assert trip is not None
    trip.state = "completed"
    _save(persistence, trip)

    with pytest.raises(RuntimeError, match="planned/running"):
        record_trip_update(
            persistence,
            engine,
            backend,
            entities=entities,
            trip_id=entities.trip_a_id,
            sequence=1,
            delay_seconds=0,
        )


@pytest.mark.parametrize(
    ("sequence", "stop_sequence", "latitude", "longitude", "message"),
    [
        (0, 1, 0.0, 0.0, "sequence must be positive"),
        (1, 0, 0.0, 0.0, "stop_sequence must be positive"),
        (1, 1, math.nan, 0.0, "coordinates are invalid"),
        (1, 1, 91.0, 0.0, "coordinates are invalid"),
        (1, 1, 0.0, 181.0, "coordinates are invalid"),
    ],
)
def test_vehicle_position_validates_input(
    sequence, stop_sequence, latitude, longitude, message
):
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(ValueError, match=message):
        record_vehicle_position(
            persistence,
            engine,
            entities=entities,
            trip_id=entities.trip_a_id,
            sequence=sequence,
            observed_at=TRIP_A_START,
            stop_sequence=stop_sequence,
            latitude=latitude,
            longitude=longitude,
        )


def test_vehicle_position_requires_assigned_vehicle_even_when_trip_is_running():
    persistence, entities, engine, backend = _runtime()
    _start_trip_a(persistence, entities, engine, backend, assign=False)

    with pytest.raises(RuntimeError, match="vehicle assigned to trip"):
        record_vehicle_position(
            persistence,
            engine,
            entities=entities,
            trip_id=entities.trip_a_id,
            sequence=1,
            observed_at=TRIP_A_START + timedelta(minutes=1),
            stop_sequence=1,
            latitude=-16.68,
            longitude=-49.25,
        )


def test_vehicle_position_replay_is_idempotent_and_conflict_is_rejected():
    persistence, entities, engine, backend = _runtime()
    _start_trip_a(persistence, entities, engine, backend)

    kwargs = dict(
        persistence=persistence,
        engine=engine,
        entities=entities,
        trip_id=entities.trip_a_id,
        sequence=3,
        observed_at=TRIP_A_START + timedelta(minutes=5),
        stop_sequence=2,
        latitude=-16.68,
        longitude=-49.25,
    )
    first = record_vehicle_position(**kwargs)
    second = record_vehicle_position(**kwargs)
    assert first is not None and second is not None and first.id == second.id

    with pytest.raises(ValueError, match="different observation"):
        record_vehicle_position(**{**kwargs, "latitude": -16.70})


def test_realtime_view_without_position_is_explicitly_stale():
    persistence, entities, _, _ = _runtime()

    view = realtime_vehicle_view(
        persistence,
        entities=entities,
        as_of=ORIGIN,
    )

    assert view["position"] is None
    assert view["stale"] is True
    assert view["active_trip_id"] is None


def test_cancel_trip_is_idempotent_and_completed_trip_is_not_cancelled():
    persistence, entities, engine, _ = _runtime()
    assert cancel_trip(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
    )
    before = tuple(persistence.events())
    assert cancel_trip(
        persistence,
        engine,
        entities=entities,
        trip_id=entities.trip_a_id,
    )
    assert tuple(persistence.events()) == before

    trip_b = persistence.entity("transit_scheduled_trip", entities.trip_b_id)
    assert trip_b is not None
    trip_b.state = "completed"
    _save(persistence, trip_b)
    assert (
        cancel_trip(
            persistence,
            engine,
            entities=entities,
            trip_id=entities.trip_b_id,
        )
        is False
    )


@pytest.mark.parametrize(
    ("alert_key", "trip_ids", "start_offset", "end_offset", "message"),
    [
        ("", ("trip",), 1, 2, "alert_key"),
        ("empty", (), 1, 2, "affected trips"),
        ("range", ("trip",), 2, 1, "end must be after start"),
    ],
)
def test_service_alert_validates_identity_scope_and_time(
    alert_key, trip_ids, start_offset, end_offset, message
):
    persistence, entities, engine, _ = _runtime()
    resolved = tuple(
        entities.trip_a_id if trip_id == "trip" else trip_id
        for trip_id in trip_ids
    )

    with pytest.raises(ValueError, match=message):
        schedule_service_alert(
            persistence,
            engine,
            alert_key=alert_key,
            affected_trip_ids=resolved,
            start_at=ORIGIN + timedelta(hours=start_offset),
            end_at=ORIGIN + timedelta(hours=end_offset),
        )


def test_service_alert_replay_rejects_scope_change():
    persistence, entities, engine, _ = _runtime()
    start = ORIGIN + timedelta(minutes=10)
    end = ORIGIN + timedelta(hours=2)
    schedule_service_alert(
        persistence,
        engine,
        alert_key="scope",
        affected_trip_ids=(entities.trip_a_id,),
        start_at=start,
        end_at=end,
    )

    with pytest.raises(ValueError, match="scope cannot change"):
        schedule_service_alert(
            persistence,
            engine,
            alert_key="scope",
            affected_trip_ids=(entities.trip_b_id,),
            start_at=start,
            end_at=end,
        )


def test_cancel_service_alert_is_idempotent_and_cleared_alert_is_not_cancelled():
    persistence, entities, engine, _ = _runtime()
    start = ORIGIN + timedelta(minutes=10)
    end = ORIGIN + timedelta(hours=2)

    alert = schedule_service_alert(
        persistence,
        engine,
        alert_key="cancel-idempotent",
        affected_trip_ids=(entities.trip_a_id,),
        start_at=start,
        end_at=end,
    )
    assert cancel_service_alert(
        persistence,
        engine,
        alert_key="cancel-idempotent",
    )
    before = tuple(persistence.events())
    assert cancel_service_alert(
        persistence,
        engine,
        alert_key="cancel-idempotent",
    )
    assert tuple(persistence.events()) == before

    cleared = schedule_service_alert(
        persistence,
        engine,
        alert_key="already-cleared",
        affected_trip_ids=(entities.trip_b_id,),
        start_at=start,
        end_at=end,
    )
    cleared.state = "cleared"
    _save(persistence, cleared)
    assert (
        cancel_service_alert(
            persistence,
            engine,
            alert_key="already-cleared",
        )
        is False
    )
