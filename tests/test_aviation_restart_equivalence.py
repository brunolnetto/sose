from sose.backends.simpy import SimPyBackend
from sose.examples.aviation.simulation import (
    ORIGIN,
    build_runtime,
    complete_aog_maintenance,
    flow_correlation_id,
    land_flight,
    maintenance_work_order_id,
    reconcile_aog_maintenance,
    reconcile_departure,
    reconcile_inspection,
    reconcile_part_issue,
    schedule_departure,
    schedule_rotation,
    seed_reference,
    seed_spare_part,
)
from sose.persistence.memory import MemoryPersistence


def test_departure_schedule_survives_restart():
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
    restart_at = backend.now

    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)
    assert len(persistence.scheduled_work()) == 1

    rebuilt_backend.run_until(due_at)
    flight = persistence.entity("aviation_flight", entities.leg1_id)
    assert flight is not None and flight.state == "due"
    assert persistence.scheduled_work() == ()


def test_pending_crew_demand_survives_restart():
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

    engine.resources.request(
        backend,
        resource_name="flight_crew",
        request_id="crew-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    ) is False
    request_id = f"flight-crew:{entities.leg1_id}"
    assert any(
        demand.request_id == request_id
        for demand in persistence.resource_demands()
    )

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    snapshot = rebuilt_backend.resource_snapshot("flight_crew")
    assert snapshot.in_use == 1
    assert snapshot.queued == 1

    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "crew-blocker"
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
        flight_id=entities.leg1_id,
    )
    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "airborne"


def test_airborne_flight_reconciles_aircraft_after_dispatch_crash():
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

    flight = persistence.entity("aviation_flight", entities.leg1_id)
    aircraft = persistence.entity("aviation_aircraft", entities.aircraft_id)
    crew = persistence.entity(
        "aviation_crew_assignment",
        entities.leg1_crew_id,
    )
    correlation_id = flow_correlation_id(entities.aircraft_id)
    assert flight is not None and aircraft is not None and crew is not None

    engine.resources.request(
        backend,
        resource_name="flight_crew",
        request_id=f"flight-crew:{flight.id}",
        requested_at=backend.now,
        priority=100,
    )
    backend.run_until(backend.now)
    for entity, event, key in (
        (crew, "reserve", ("aviation-crash", crew.id, "reserve")),
        (crew, "activate", ("aviation-crash", crew.id, "activate")),
        (flight, "mark_ready", ("aviation-crash", flight.id, "ready")),
        (aircraft, "assign", ("aviation-crash", aircraft.id, "assign")),
    ):
        current = persistence.entity(entity.entity_type, entity.id)
        command = engine.context.commands.create(
            event,
            target=current,
            correlation_id=correlation_id,
            key=key,
        )
        engine.dispatch(command)

    flight = persistence.entity("aviation_flight", entities.leg1_id)
    command = engine.context.commands.create(
        "depart",
        target=flight,
        correlation_id=correlation_id,
        key=("aviation-crash", flight.id, "depart"),
    )
    engine.dispatch(command)

    assert persistence.entity(
        "aviation_flight",
        entities.leg1_id,
    ).state == "airborne"
    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "assigned"

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "airborne"


def _prepare_aog_with_part():
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
    land_flight(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
        fail=True,
    ) is False
    seed_spare_part(persistence, engine, backend)
    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    return persistence, entities, engine, backend


def test_committed_maintenance_queue_selection_survives_restart():
    persistence, entities, engine, backend = _prepare_aog_with_part()
    work_id = maintenance_work_order_id(entities.leg1_id)
    request_id = f"maintenance-pick:{work_id}"

    engine.stores.get(
        backend,
        store_name="maintenance_queue",
        request_id=request_id,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)
    result = engine.stores.result(request_id)
    assert result is not None
    assert result.item.value["work_order_id"] == work_id
    assert not any(
        item.store_name == "maintenance_queue"
        for item in persistence.store_items()
    )

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    assert reconcile_aog_maintenance(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert persistence.entity(
        "aviation_maintenance_work_order",
        work_id,
    ).state == "in_progress"


def test_completed_maintenance_reconciles_aircraft_and_flight_after_restart():
    persistence, entities, engine, backend = _prepare_aog_with_part()
    assert reconcile_aog_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )

    work = persistence.entity(
        "aviation_maintenance_work_order",
        maintenance_work_order_id(entities.leg1_id),
    )
    command = engine.context.commands.create(
        "complete",
        target=work,
        correlation_id=flow_correlation_id(entities.aircraft_id),
        key=("aviation-crash-maintenance", work.id, "complete"),
    )
    engine.dispatch(command)

    assert persistence.entity(
        "aviation_maintenance_work_order",
        work.id,
    ).state == "completed"
    assert persistence.entity(
        "aviation_aircraft",
        entities.aircraft_id,
    ).state == "maintenance"
    assert persistence.entity(
        "aviation_flight",
        entities.leg1_id,
    ).state == "inspection"

    restart_at = backend.now
    _, rebuilt_engine = build_runtime(persistence, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    assert complete_aog_maintenance(
        persistence,
        rebuilt_engine,
        rebuilt_backend,
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
