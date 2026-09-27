from sose.backends.simpy import SimPyBackend
from sose.examples.aviation.scenarios import (
    crew_shortage_scenario,
    departure_weather_scenario,
)
from sose.examples.aviation.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_departure,
    schedule_departure,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_weather_delays_due_flight_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(departure_weather_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    due_at = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=context.clock.step,
    )
    engine.advance_tick()
    backend.run_until(due_at)
    assert context.scenarios.attribute(
        "aviation.departure.available",
        True,
    ) is False

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    ) is False
    assert persistence.entity(
        "aviation_flight",
        entities.leg1_id,
    ).state == "delayed"
    assert persistence.resource_demands() == ()

    for _ in range(3):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )


def test_crew_shortage_does_not_leave_stale_crew_demand():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(crew_shortage_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    due_at = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=context.clock.step,
    )
    engine.advance_tick()
    backend.run_until(due_at)
    assert context.scenarios.attribute("aviation.crew.available", True) is False

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    ) is False
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()

    for _ in range(2):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
