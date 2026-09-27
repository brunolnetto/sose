import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.airports.simulation import (
    ORIGIN,
    build_runtime,
    flow_correlation_id,
    queue_departure,
    reconcile_baggage,
    reconcile_gate,
    reconcile_ground_service,
    schedule_arrival,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _arrived_at_gate():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    arrival_at = schedule_arrival(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(arrival_at)
    return persistence, entities, engine, backend


def test_occupied_gate_reconciles_turnaround_after_crash():
    persistence, entities, engine, backend = _arrived_at_gate()
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assignment = persistence.entity(
        "airport_gate_assignment",
        entities.gate_assignment_id,
    )
    assert turnaround is not None and assignment is not None

    engine.resources.request(
        backend,
        resource_name="gate",
        request_id=f"gate:{turnaround.id}",
        requested_at=backend.now,
        priority=50,
    )
    backend.run_until(backend.now)

    correlation_id = flow_correlation_id(turnaround.id)
    for event in ("reserve", "occupy"):
        command = engine.context.commands.create(
            event,
            target=assignment,
            correlation_id=correlation_id,
            key=("airport-crash-gate", assignment.id, event),
        )
        engine.dispatch(command)
        assignment = persistence.entity(
            "airport_gate_assignment",
            assignment.id,
        )
        assert assignment is not None

    assert assignment.state == "occupied"
    assert turnaround.state == "arrived"

    assert reconcile_gate(
        persistence, engine, backend, entities=entities
    )
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None and turnaround.state == "gate_assigned"


def test_completed_ground_task_reconciles_turnaround_and_releases_team():
    persistence, entities, engine, backend = _arrived_at_gate()
    assert reconcile_gate(persistence, engine, backend, entities=entities)

    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    task = persistence.entity(
        "airport_ground_service_task",
        entities.service_task_id,
    )
    assert turnaround is not None and task is not None
    correlation_id = flow_correlation_id(turnaround.id)

    for event in ("start_deboarding", "start_servicing"):
        command = engine.context.commands.create(
            event,
            target=turnaround,
            correlation_id=correlation_id,
            key=("airport-crash-service", turnaround.id, event),
        )
        engine.dispatch(command)
        turnaround = persistence.entity(
            "airport_flight_turnaround",
            turnaround.id,
        )
        assert turnaround is not None

    request_id = f"ground-team:{task.id}"
    engine.resources.request(
        backend,
        resource_name="ground_team",
        request_id=request_id,
        requested_at=backend.now,
        priority=100,
    )
    backend.run_until(backend.now)
    for event in ("start", "complete"):
        command = engine.context.commands.create(
            event,
            target=task,
            correlation_id=correlation_id,
            key=("airport-crash-service", task.id, event),
        )
        engine.dispatch(command)
        task = persistence.entity(
            "airport_ground_service_task",
            task.id,
        )
        assert task is not None

    assert task.state == "completed"
    assert turnaround.state == "servicing"

    assert reconcile_ground_service(
        persistence, engine, backend, entities=entities
    )
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None and turnaround.state == "boarding"
    assert not any(
        r.request_id == request_id
        for r in persistence.resource_reservations()
    )


def test_departure_queue_rejects_missing_operational_evidence():
    persistence, entities, engine, backend = _arrived_at_gate()
    assert reconcile_gate(persistence, engine, backend, entities=entities)

    with pytest.raises(RuntimeError, match="FlightTurnaround\(boarding\)"):
        queue_departure(
            persistence,
            engine,
            backend,
            entities=entities,
        )

    assert reconcile_ground_service(
        persistence, engine, backend, entities=entities
    )
    with pytest.raises(RuntimeError, match="BaggageFlow\(ready\)"):
        queue_departure(
            persistence,
            engine,
            backend,
            entities=entities,
        )

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


def test_departure_reconciler_never_consumes_another_flights_priority_item():
    from sose.examples.airports.simulation import (
        queue_departure,
        reconcile_baggage,
        reconcile_departure,
        reconcile_ground_service,
        schedule_departure_slot,
    )

    persistence, entities, engine, backend = _arrived_at_gate()
    assert reconcile_gate(persistence, engine, backend, entities=entities)
    assert reconcile_ground_service(
        persistence, engine, backend, entities=entities
    )
    assert reconcile_baggage(
        persistence, engine, entities=entities
    )
    queue_departure(
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
    backend.run_until(slot_at)

    engine.stores.put(
        backend,
        store_name="departure_queue",
        item_id="departure:other-flight",
        value={"turnaround_id": "other-flight"},
        priority=1,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    items = {
        item.item_id: item
        for item in persistence.store_items()
        if item.store_name == "departure_queue"
    }
    assert "departure:other-flight" in items
    assert f"departure:{entities.turnaround_id}" in items


def test_reallocated_gate_releases_stale_reservation_after_crash():
    persistence, entities, engine, backend = _arrived_at_gate()
    assert reconcile_gate(persistence, engine, backend, entities=entities)

    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assignment = persistence.entity(
        "airport_gate_assignment",
        entities.gate_assignment_id,
    )
    assert turnaround is not None and assignment is not None

    old_reservation = next(
        r
        for r in persistence.resource_reservations()
        if r.request_id == f"gate:{entities.turnaround_id}"
    )
    correlation_id = flow_correlation_id(turnaround.id)

    command = engine.context.commands.create(
        "reallocate_gate",
        target=turnaround,
        correlation_id=correlation_id,
        key=("airport-crash-reallocation", turnaround.id, "turnaround"),
    )
    engine.dispatch(command)
    command = engine.context.commands.create(
        "reallocate",
        target=assignment,
        correlation_id=correlation_id,
        key=("airport-crash-reallocation", assignment.id, "assignment"),
    )
    engine.dispatch(command)

    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "gate_hold"
    assert persistence.entity(
        "airport_gate_assignment",
        entities.gate_assignment_id,
    ).state == "reallocated"
    assert any(
        r.reservation_id == old_reservation.reservation_id
        for r in persistence.resource_reservations()
    )

    assert reconcile_gate(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    assignment = persistence.entity(
        "airport_gate_assignment",
        entities.gate_assignment_id,
    )
    current = next(
        r
        for r in persistence.resource_reservations()
        if r.request_id == f"gate:{entities.turnaround_id}"
    )
    assert assignment is not None and assignment.state == "occupied"
    assert current.sequence > old_reservation.sequence


def test_waiting_slot_recovers_missing_departure_queue_item():
    persistence, entities, engine, backend = _arrived_at_gate()
    assert reconcile_gate(persistence, engine, backend, entities=entities)
    assert reconcile_ground_service(
        persistence, engine, backend, entities=entities
    )
    assert reconcile_baggage(
        persistence, engine, entities=entities
    )

    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None and turnaround.state == "boarding"
    command = engine.context.commands.create(
        "start_boarding",
        target=turnaround,
        correlation_id=flow_correlation_id(turnaround.id),
        key=("airport-crash-queue", turnaround.id, "start-boarding"),
    )
    engine.dispatch(command)

    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "waiting_slot"
    assert not any(
        item.store_name == "departure_queue"
        for item in persistence.store_items()
    )

    queue_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert any(
        item.item_id == f"departure:{entities.turnaround_id}"
        for item in persistence.store_items()
    )


def test_baggage_delay_reconciles_turnaround_wait_state_after_crash():
    persistence, entities, engine, backend = _arrived_at_gate()
    assert reconcile_gate(persistence, engine, backend, entities=entities)
    assert reconcile_ground_service(
        persistence, engine, backend, entities=entities
    )

    baggage = persistence.entity(
        "airport_baggage_flow",
        entities.baggage_flow_id,
    )
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert baggage is not None and turnaround is not None

    for event in ("start", "delay"):
        command = engine.context.commands.create(
            event,
            target=baggage,
            correlation_id=flow_correlation_id(turnaround.id),
            key=("airport-crash-baggage", baggage.id, event),
        )
        engine.dispatch(command)
        baggage = persistence.entity(
            "airport_baggage_flow",
            entities.baggage_flow_id,
        )
        assert baggage is not None

    assert baggage.state == "delayed"
    assert turnaround.state == "boarding"

    assert reconcile_baggage(
        persistence,
        engine,
        entities=entities,
    )
    baggage = persistence.entity(
        "airport_baggage_flow",
        entities.baggage_flow_id,
    )
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert baggage is not None and baggage.state == "ready"
    assert turnaround is not None and turnaround.state == "boarding"

    events = [
        event.name
        for event in persistence.events()
        if event.entity_id == entities.turnaround_id
    ]
    assert "baggage_delayed" in events
    assert "baggage_ready" in events
