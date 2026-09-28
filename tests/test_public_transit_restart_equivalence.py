from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.public_transit.simulation import (
    ORIGIN,
    begin_trip,
    build_runtime,
    record_trip_delay,
    record_vehicle_position,
    schedule_service_alert,
    seed_reference,
    service_alert_id,
    trip_update_id,
    vehicle_position_id,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def test_projection_occurrences_and_alert_window_survive_rebuild():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    begin_trip(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
    )
    record_trip_delay(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=1,
        delay_minutes=15,
    )
    record_vehicle_position(
        persistence,
        engine,
        entities=entities,
        trip_id_value=entities.first_trip_id,
        sequence=1,
        measured_at=ORIGIN + timedelta(seconds=30),
        latitude=-16.68,
        longitude=-49.25,
    )
    alert, start_at, end_at = schedule_service_alert(
        persistence,
        engine,
        backend,
        entities=entities,
        incident_key="blocked-segment",
        affected_trip_ids=(entities.first_trip_id, entities.second_trip_id),
        start_delay=timedelta(minutes=2),
        duration=timedelta(minutes=5),
    )

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    second = persistence.entity("transit_scheduled_trip", entities.second_trip_id)
    assert second is not None and second.attributes["current_delay_minutes"] == 5
    assert persistence.entity(
        "transit_trip_update",
        trip_update_id(entities.first_trip_id, 1),
    ).state == "committed"
    assert persistence.entity(
        "transit_vehicle_position",
        vehicle_position_id(entities.vehicle_id, 1),
    ).state == "committed"

    rebuilt.backend.run_until(start_at)
    assert persistence.entity("transit_service_alert", alert.id).state == "active"
    rebuilt.backend.run_until(end_at)
    assert persistence.entity(
        "transit_service_alert",
        service_alert_id("blocked-segment"),
    ).state == "resolved"
    assert persistence.scheduled_work() == ()
