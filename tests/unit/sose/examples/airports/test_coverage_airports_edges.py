from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.airports.simulation import (
    ORIGIN,
    _entity,
    build_runtime,
    queue_departure,
    reallocate_gate,
    reconcile_baggage,
    reconcile_departure,
    reconcile_gate,
    reconcile_ground_service,
    schedule_arrival,
    schedule_departure_slot,
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


def test_missing_airport_entity_guard_is_observable():
    with pytest.raises(RuntimeError, match="was not persisted"):
        _entity(MemoryPersistence(), "airport_flight_turnaround", "missing")


def test_arrival_schedule_is_idempotent_and_terminal_turnaround_returns_now():
    persistence, entities, engine, backend = _runtime()

    first = schedule_arrival(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=timedelta(hours=1),
    )
    second = schedule_arrival(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=timedelta(hours=9),
    )
    assert second == first
    assert len(persistence.scheduled_work()) == 1

    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None
    turnaround.state = "arrived"
    _save(persistence, turnaround)

    assert schedule_arrival(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=timedelta(hours=10),
    ) == backend.now
    assert len(persistence.scheduled_work()) == 1


def test_gate_reconciliation_returns_false_before_arrival():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_gate(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_gate_outage_holds_arrival_without_owning_capacity(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None
    turnaround.state = "arrived"
    _save(persistence, turnaround)

    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "airport.gate.available"
        else default,
    )

    assert reconcile_gate(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    persisted = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert persisted is not None and persisted.state == "gate_hold"
    assert engine.resources.has_request(f"gate:{entities.turnaround_id}") is False


def test_gate_reallocation_rejects_unassigned_turnaround():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="requires assigned turnaround"):
        reallocate_gate(
            persistence,
            engine,
            backend,
            entities=entities,
            new_gate="G2",
        )


def test_ground_service_returns_false_before_gate_assignment():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_ground_service(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_ground_service_waits_with_durable_team_demand():
    persistence, entities, engine, backend = _runtime()
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None
    turnaround.state = "gate_assigned"
    _save(persistence, turnaround)

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="ground_team",
        request_id="ground:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_ground_service(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    request_id = f"ground-team:{entities.service_task_id}"
    assert engine.resources.has_request(request_id)
    assert engine.resources.reservation_for(request_id) is None


def test_baggage_reconciliation_returns_false_outside_boarding_states():
    persistence, entities, engine, _ = _runtime()

    assert reconcile_baggage(
        persistence,
        engine,
        entities=entities,
    ) is False


def test_departure_slot_schedule_is_idempotent_and_terminal_slot_returns_now():
    persistence, entities, engine, backend = _runtime()

    first = schedule_departure_slot(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=timedelta(hours=1),
    )
    second = schedule_departure_slot(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=timedelta(hours=8),
    )
    assert second == first
    assert len(persistence.scheduled_work()) == 1

    terminal_persistence, terminal_entities, terminal_engine, terminal_backend = _runtime()
    slot = terminal_persistence.entity(
        "airport_departure_slot",
        terminal_entities.departure_slot_id,
    )
    assert slot is not None
    slot.state = "consumed"
    _save(terminal_persistence, slot)

    assert schedule_departure_slot(
        terminal_persistence,
        terminal_engine,
        terminal_backend,
        entities=terminal_entities,
        delay=timedelta(hours=10),
    ) == terminal_backend.now
    assert terminal_persistence.scheduled_work() == ()


def test_departure_queue_independently_requires_completed_ground_service():
    persistence, entities, engine, backend = _runtime()
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    baggage = persistence.entity("airport_baggage_flow", entities.baggage_flow_id)
    task = persistence.entity(
        "airport_ground_service_task",
        entities.service_task_id,
    )
    assert turnaround is not None and baggage is not None and task is not None

    turnaround.state = "boarding"
    baggage.state = "ready"
    task.state = "pending"
    _save(persistence, turnaround)
    _save(persistence, baggage)
    _save(persistence, task)

    with pytest.raises(RuntimeError, match=r"GroundServiceTask\(completed\)"):
        queue_departure(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_departure_returns_false_outside_waiting_slot():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_departure_waits_until_slot_is_due():
    persistence, entities, engine, backend = _runtime()
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    slot = persistence.entity("airport_departure_slot", entities.departure_slot_id)
    assert turnaround is not None and slot is not None
    turnaround.state = "waiting_slot"
    slot.state = "scheduled"
    _save(persistence, turnaround)
    _save(persistence, slot)

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert engine.resources.has_request(f"tug:{entities.turnaround_id}") is False


def test_departure_outage_delays_due_slot_without_tug_ownership(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    slot = persistence.entity("airport_departure_slot", entities.departure_slot_id)
    assert turnaround is not None and slot is not None
    turnaround.state = "waiting_slot"
    slot.state = "due"
    _save(persistence, turnaround)
    _save(persistence, slot)

    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "airport.departure.available"
        else default,
    )

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    persisted_slot = persistence.entity(
        "airport_departure_slot",
        entities.departure_slot_id,
    )
    assert persisted_slot is not None and persisted_slot.state == "delayed"
    assert engine.resources.has_request(f"tug:{entities.turnaround_id}") is False


def test_departure_waits_with_durable_tug_demand():
    persistence, entities, engine, backend = _runtime()
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    slot = persistence.entity("airport_departure_slot", entities.departure_slot_id)
    assert turnaround is not None and slot is not None
    turnaround.state = "waiting_slot"
    slot.state = "due"
    _save(persistence, turnaround)
    _save(persistence, slot)

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="tug",
        request_id="tug:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    request_id = f"tug:{entities.turnaround_id}"
    assert engine.resources.has_request(request_id)
    assert engine.resources.reservation_for(request_id) is None


def test_departure_without_queue_item_releases_tug_ownership():
    persistence, entities, engine, backend = _runtime()
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    slot = persistence.entity("airport_departure_slot", entities.departure_slot_id)
    assert turnaround is not None and slot is not None
    turnaround.state = "waiting_slot"
    slot.state = "due"
    _save(persistence, turnaround)
    _save(persistence, slot)

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert engine.resources.has_request(f"tug:{entities.turnaround_id}") is False


def test_departed_replay_releases_gate_and_tug_ownership():
    persistence, entities, engine, backend = _runtime()
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    gate = persistence.entity(
        "airport_gate_assignment",
        entities.gate_assignment_id,
    )
    assert turnaround is not None and gate is not None
    turnaround.state = "departed"
    gate.state = "occupied"
    _save(persistence, turnaround)
    _save(persistence, gate)

    gate_reservation = engine.resources.ensure_requested(
        backend,
        resource_name="gate",
        request_id=f"gate:{entities.turnaround_id}",
        requested_at=backend.now,
    )
    tug_reservation = engine.resources.ensure_requested(
        backend,
        resource_name="tug",
        request_id=f"tug:{entities.turnaround_id}",
        requested_at=backend.now,
    )
    assert gate_reservation is not None and tug_reservation is not None

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    persisted_gate = persistence.entity(
        "airport_gate_assignment",
        entities.gate_assignment_id,
    )
    assert persisted_gate is not None and persisted_gate.state == "released"
    assert engine.resources.has_request(f"gate:{entities.turnaround_id}") is False
    assert engine.resources.has_request(f"tug:{entities.turnaround_id}") is False
