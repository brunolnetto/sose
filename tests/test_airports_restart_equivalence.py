from sose.backends.simpy import SimPyBackend
from sose.examples.airports.simulation import (
    ORIGIN,
    build_runtime,
    flow_correlation_id,
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
from sose.testing.restart import restart_reference_runtime


def test_pending_arrival_schedule_survives_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend_before = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend_before)

    due_at = schedule_arrival(
        persistence,
        engine,
        backend_before,
        entities=entities,
    )
    restart_at = backend_before.now

    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)
    assert len(persistence.scheduled_work()) == 1

    rebuilt_backend.run_until(due_at)
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    assert turnaround is not None and turnaround.state == "arrived"
    assert persistence.scheduled_work() == ()


def test_pending_gate_demand_survives_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    arrival_at = schedule_arrival(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(arrival_at)

    engine.resources.request(
        backend,
        resource_name="gate",
        request_id="gate-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert reconcile_gate(
        persistence, engine, backend, entities=entities
    ) is False
    assert any(
        d.request_id == f"gate:{entities.turnaround_id}"
        for d in persistence.resource_demands()
    )

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt_engine = rebuilt.engine
    rebuilt_backend = rebuilt.backend

    snapshot = rebuilt_backend.resource_snapshot("gate")
    assert snapshot.in_use == 1
    assert snapshot.queued == 1

    blocker = next(
        r for r in persistence.resource_reservations()
        if r.request_id == "gate-blocker"
    )
    rebuilt_engine.resources.release(
        rebuilt_backend,
        blocker.reservation_id,
    )
    rebuilt_backend.run_until(rebuilt_backend.now)

    assert reconcile_gate(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
        entities=entities,
    )
    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "gate_assigned"


def _prepare_waiting_slot(persistence):
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    arrival_at = schedule_arrival(
        persistence, engine, backend, entities=entities
    )
    slot_at = schedule_departure_slot(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(arrival_at)
    assert reconcile_gate(persistence, engine, backend, entities=entities)
    assert reconcile_ground_service(
        persistence, engine, backend, entities=entities
    )
    assert reconcile_baggage(persistence, engine, entities=entities)
    queue_departure(persistence, engine, backend, entities=entities)
    backend.run_until(slot_at)
    return entities, engine, backend


def test_pending_tug_demand_survives_restart():
    persistence = MemoryPersistence()
    entities, engine, backend = _prepare_waiting_slot(persistence)

    engine.resources.request(
        backend,
        resource_name="tug",
        request_id="tug-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert any(
        d.request_id == f"tug:{entities.turnaround_id}"
        for d in persistence.resource_demands()
    )

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    snapshot = rebuilt_backend.resource_snapshot("tug")
    assert snapshot.in_use == 1
    assert snapshot.queued == 1

    blocker = next(
        r for r in persistence.resource_reservations()
        if r.request_id == "tug-blocker"
    )
    rebuilt_engine.resources.release(
        rebuilt_backend,
        blocker.reservation_id,
    )
    rebuilt_backend.run_until(rebuilt_backend.now)

    assert reconcile_departure(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
        entities=entities,
    )
    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "departed"


def test_consumed_slot_and_pushback_recover_after_restart():
    persistence = MemoryPersistence()
    entities, engine, backend = _prepare_waiting_slot(persistence)
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    slot = persistence.entity(
        "airport_departure_slot",
        entities.departure_slot_id,
    )
    assert turnaround is not None and slot is not None

    correlation_id = flow_correlation_id(turnaround.id)
    command = engine.context.commands.create(
        "slot_ready",
        target=turnaround,
        correlation_id=correlation_id,
        key=("airport-crash", turnaround.id, "slot-ready"),
    )
    engine.dispatch(command)
    command = engine.context.commands.create(
        "consume",
        target=slot,
        correlation_id=correlation_id,
        key=("airport-crash", slot.id, "consume"),
    )
    engine.dispatch(command)

    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "pushback"
    assert persistence.entity(
        "airport_departure_slot",
        entities.departure_slot_id,
    ).state == "consumed"

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    assert reconcile_departure(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
        entities=entities,
    )
    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "departed"


def test_committed_departure_queue_selection_survives_restart():
    persistence = MemoryPersistence()
    entities, engine, backend = _prepare_waiting_slot(persistence)

    request_id = "departure-pick:departure"
    engine.stores.get(
        backend,
        store_name="departure_queue",
        request_id=request_id,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    result = engine.stores.result(request_id)
    assert result is not None
    assert result.item.value["turnaround_id"] == entities.turnaround_id
    assert not any(
        item.store_name == "departure_queue"
        for item in persistence.store_items()
    )
    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "waiting_slot"

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    assert reconcile_departure(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
        entities=entities,
    )
    assert persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    ).state == "departed"
