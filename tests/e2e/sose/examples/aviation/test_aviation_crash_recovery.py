from sose.backends.simpy import SimPyBackend
from sose.examples.aviation.simulation import (
    ORIGIN,
    build_runtime,
    flow_correlation_id,
    inspection_id,
    land_flight,
    maintenance_work_order_id,
    reconcile_departure,
    reconcile_inspection,
    schedule_departure,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_passed_inspection_reconciles_aircraft_and_flight_after_crash():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    due_at = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=engine.context.clock.step,
    )
    backend.run_until(due_at)
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

    correlation_id = flow_correlation_id(entities.aircraft_id)
    for event in ("begin", "pass_inspection"):
        inspection = persistence.entity(
            "aviation_inspection",
            inspection_id(entities.leg1_id),
        )
        command = engine.context.commands.create(
            event,
            target=inspection,
            correlation_id=correlation_id,
            key=("aviation-crash-inspection", inspection.id, event),
        )
        engine.dispatch(command)

    assert persistence.entity(
        "aviation_inspection",
        inspection_id(entities.leg1_id),
    ).state == "passed"
    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "inspection"
    assert persistence.entity(
        "aviation_flight",
        entities.leg1_id,
    ).state == "inspection"

    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "released"
    assert persistence.entity(
        "aviation_flight",
        entities.leg1_id,
    ).state == "released"


def test_failed_inspection_reconciles_aog_and_maintenance_after_crash():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    due_at = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=engine.context.clock.step,
    )
    backend.run_until(due_at)
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

    inspection = persistence.entity(
        "aviation_inspection",
        inspection_id(entities.leg1_id),
    )
    correlation_id = flow_correlation_id(entities.aircraft_id)
    for event in ("begin", "fail_inspection"):
        inspection = persistence.entity(
            "aviation_inspection",
            inspection_id(entities.leg1_id),
        )
        command = engine.context.commands.create(
            event,
            target=inspection,
            correlation_id=correlation_id,
            key=("aviation-crash-inspection", inspection.id, event),
        )
        engine.dispatch(command)

    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "inspection"

    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
        fail=True,
    ) is False
    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "aog"
    assert persistence.entity(
        "aviation_maintenance_work_order",
        maintenance_work_order_id(entities.leg1_id),
    ) is not None


def test_landed_flight_reconciles_aircraft_and_inspection_after_crash():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    due_at = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=engine.context.clock.step,
    )
    backend.run_until(due_at)
    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )

    flight = persistence.entity("aviation_flight", entities.leg1_id)
    command = engine.context.commands.create(
        "land",
        target=flight,
        correlation_id=flow_correlation_id(entities.aircraft_id),
        key=("aviation-crash-landing", flight.id, "land"),
    )
    engine.dispatch(command)

    assert persistence.entity(
        "aviation_flight",
        entities.leg1_id,
    ).state == "landed"
    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "airborne"

    inspection = land_flight(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert inspection.state == "pending"
    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "inspection"
    assert persistence.entity(
        "aviation_flight",
        entities.leg1_id,
    ).state == "inspection"
