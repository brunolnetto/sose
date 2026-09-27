from sose.backends.simpy import SimPyBackend
from sose.examples.aviation.simulation import (
    ORIGIN,
    build_runtime,
    complete_aog_maintenance,
    land_flight,
    maintenance_work_order_id,
    part_demand_id,
    preemption_result_for,
    reconcile_aog_maintenance,
    reconcile_departure,
    reconcile_inspection,
    reconcile_part_issue,
    schedule_rotation,
    seed_reference,
    seed_spare_part,
)
from sose.persistence.memory import MemoryPersistence


def _prepare_failed_inspection():
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
    return persistence, entities, engine, backend


def test_aog_requires_part_before_maintenance_bay():
    persistence, entities, engine, backend = _prepare_failed_inspection()
    work = persistence.entity(
        "aviation_maintenance_work_order",
        maintenance_work_order_id(entities.leg1_id),
    )
    demand = persistence.entity(
        "aviation_part_demand",
        part_demand_id(entities.leg1_id),
    )
    aircraft = persistence.entity("aviation_aircraft", entities.aircraft_id)
    assert work is not None and work.state == "released"
    assert demand is not None and demand.state == "open"
    assert aircraft is not None and aircraft.state == "aog"

    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    ) is False
    assert persistence.preemptive_resource_demands() == ()

    seed_spare_part(persistence, engine, backend)
    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    demand = persistence.entity(
        "aviation_part_demand",
        part_demand_id(entities.leg1_id),
    )
    assert demand is not None and demand.state == "issued"


def test_aog_maintenance_preempts_noncritical_bay_owner():
    persistence, entities, engine, backend = _prepare_failed_inspection()
    seed_spare_part(persistence, engine, backend)
    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )

    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id="routine-maintenance",
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    backend.run_until(backend.now)
    assert any(
        r.request_id == "routine-maintenance"
        for r in persistence.preemptive_resource_reservations()
    )

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
    aircraft = persistence.entity("aviation_aircraft", entities.aircraft_id)
    assert work is not None and work.state == "in_progress"
    assert aircraft is not None and aircraft.state == "maintenance"

    request_id = f"maintenance-bay:{work.id}"
    result = preemption_result_for(
        persistence,
        preempting_request_id=request_id,
    )
    assert result is not None
    assert result.displaced_request_id == "routine-maintenance"

    assert complete_aog_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    work = persistence.entity(
        "aviation_maintenance_work_order",
        work.id,
    )
    aircraft = persistence.entity("aviation_aircraft", entities.aircraft_id)
    flight = persistence.entity("aviation_flight", entities.leg1_id)
    assert work is not None and work.state == "completed"
    assert aircraft is not None and aircraft.state == "released"
    assert flight is not None and flight.state == "released"
    assert not any(
        r.request_id == request_id
        for r in persistence.preemptive_resource_reservations()
    )


def test_aog_on_first_leg_delays_next_leg_until_maintenance_release():
    persistence, entities, engine, backend = _prepare_failed_inspection()

    leg2_work = next(
        work
        for work in persistence.scheduled_work()
        if persistence.command(work.command_id).entity_id == entities.leg2_id
    )
    backend.run_until(leg2_work.due_at)

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg2_id,
    ) is False
    leg2 = persistence.entity("aviation_flight", entities.leg2_id)
    assert leg2 is not None and leg2.state == "delayed"

    seed_spare_part(persistence, engine, backend)
    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert reconcile_aog_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert complete_aog_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg2_id,
    )
    leg2 = persistence.entity("aviation_flight", entities.leg2_id)
    assert leg2 is not None and leg2.state == "airborne"
