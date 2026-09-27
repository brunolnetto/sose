from sose.backends.simpy import SimPyBackend
from sose.examples.aviation.simulation import (
    ORIGIN,
    build_runtime,
    land_flight,
    reconcile_departure,
    reconcile_inspection,
    schedule_rotation,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_two_leg_rotation_requires_previous_leg_release():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    leg1_due, leg2_due = schedule_rotation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(leg1_due)
    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    land_flight(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )

    backend.run_until(leg2_due)
    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg2_id,
    ) is False
    leg2 = persistence.entity("aviation_flight", entities.leg2_id)
    assert leg2 is not None and leg2.state == "delayed"

    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
        fail=False,
    )
    leg1 = persistence.entity("aviation_flight", entities.leg1_id)
    aircraft = persistence.entity("aviation_aircraft", entities.aircraft_id)
    assert leg1 is not None and leg1.state == "released"
    assert aircraft is not None and aircraft.state == "released"

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg2_id,
    )
    leg2 = persistence.entity("aviation_flight", entities.leg2_id)
    assert leg2 is not None and leg2.state == "airborne"


def test_landing_releases_crew_but_not_aircraft_before_inspection():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    leg1_due, _ = schedule_rotation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(leg1_due)
    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )

    inspection = land_flight(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    crew = persistence.entity(
        "aviation_crew_assignment",
        entities.leg1_crew_id,
    )
    aircraft = persistence.entity("aviation_aircraft", entities.aircraft_id)
    flight = persistence.entity("aviation_flight", entities.leg1_id)

    assert inspection.state == "pending"
    assert crew is not None and crew.state == "released"
    assert aircraft is not None and aircraft.state == "inspection"
    assert flight is not None and flight.state == "inspection"
    assert persistence.resource_reservations() == ()
