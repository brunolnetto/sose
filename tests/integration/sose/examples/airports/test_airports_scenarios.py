from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.airports.scenarios import (
    gate_congestion_scenario,
    weather_departure_scenario,
)
from sose.examples.airports.simulation import (
    ORIGIN,
    build_runtime,
    queue_departure,
    reconcile_baggage,
    reconcile_departure,
    reconcile_gate,
    reconcile_ground_service,
    schedule_arrival,
    schedule_departure_slot,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_gate_congestion_holds_then_assigns_gate():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(gate_congestion_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    arrival_at = schedule_arrival(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=timedelta(hours=1),
    )
    backend.run_until(arrival_at)
    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute("airport.gate.available", True) is False

    assert reconcile_gate(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None and turnaround.state == "gate_hold"
    assert persistence.resource_demands() == ()

    for _ in range(3):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_gate(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "gate_assigned"


def test_weather_delays_due_slot_without_consuming_it():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(weather_departure_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    arrival_at = schedule_arrival(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    slot_at = schedule_departure_slot(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=timedelta(hours=1),
    )
    backend.run_until(arrival_at)
    assert reconcile_gate(persistence, engine, backend, entities=entities)
    assert reconcile_ground_service(
        persistence, engine, backend, entities=entities
    )
    assert reconcile_baggage(persistence, engine, entities=entities)
    queue_departure(persistence, engine, backend, entities=entities)

    engine.advance_tick()
    backend.run_until(slot_at)
    assert context.scenarios.attribute(
        "airport.departure.available", True
    ) is False

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    slot = persistence.entity(
        "airport_departure_slot",
        entities.departure_slot_id,
    )
    assert slot is not None and slot.state == "delayed"
    assert persistence.resource_demands() == ()

    for _ in range(4):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "departed"
