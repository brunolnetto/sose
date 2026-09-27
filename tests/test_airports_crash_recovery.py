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
