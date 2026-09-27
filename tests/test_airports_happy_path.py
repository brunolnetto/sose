from sose.backends.simpy import SimPyBackend
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


def _prepare_boarding():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
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
    )
    backend.run_until(arrival_at)
    assert reconcile_gate(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert reconcile_ground_service(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    return persistence, entities, engine, backend, slot_at


def test_nominal_turnaround_depends_on_slot_queue_and_tug():
    persistence, entities, engine, backend, slot_at = _prepare_boarding()

    assert reconcile_baggage(
        persistence,
        engine,
        entities=entities,
    )
    queue_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None and turnaround.state == "waiting_slot"
    assert len(persistence.store_items()) == 1

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    backend.run_until(slot_at)
    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    slot = persistence.entity(
        "airport_departure_slot",
        entities.departure_slot_id,
    )
    gate = persistence.entity(
        "airport_gate_assignment",
        entities.gate_assignment_id,
    )
    assert turnaround is not None and turnaround.state == "departed"
    assert slot is not None and slot.state == "consumed"
    assert gate is not None and gate.state == "released"
    assert persistence.resource_reservations() == ()


def test_baggage_delay_blocks_departure_queue_until_recovered():
    persistence, entities, engine, backend, _ = _prepare_boarding()

    assert reconcile_baggage(
        persistence,
        engine,
        entities=entities,
        delayed=True,
    ) is False
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    baggage = persistence.entity(
        "airport_baggage_flow",
        entities.baggage_flow_id,
    )
    assert turnaround is not None and turnaround.state == "waiting_baggage"
    assert baggage is not None and baggage.state == "delayed"

    assert reconcile_baggage(
        persistence,
        engine,
        entities=entities,
    )
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None and turnaround.state == "boarding"

    queue_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "waiting_slot"


def test_gate_reallocation_releases_old_capacity_and_reassigns_turnaround():
    from sose.examples.airports.simulation import reallocate_gate

    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    arrival_at = schedule_arrival(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(arrival_at)
    assert reconcile_gate(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    old_reservation = next(
        r
        for r in persistence.resource_reservations()
        if r.request_id == f"gate:{entities.turnaround_id}"
    )

    assert reallocate_gate(
        persistence,
        engine,
        backend,
        entities=entities,
        new_gate="G2",
    )

    assignment = persistence.entity(
        "airport_gate_assignment",
        entities.gate_assignment_id,
    )
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert assignment is not None and assignment.state == "occupied"
    assert assignment.attributes["gate"] == "G2"
    assert turnaround is not None and turnaround.state == "gate_assigned"

    current = next(
        r
        for r in persistence.resource_reservations()
        if r.request_id == f"gate:{entities.turnaround_id}"
    )
    assert current.reservation_id != old_reservation.reservation_id
