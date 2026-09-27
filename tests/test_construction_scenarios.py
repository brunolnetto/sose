from sose.backends.simpy import SimPyBackend
from sose.examples.construction.execution import (
    reconcile_dependency,
    request_execution_resources,
)
from sose.examples.construction.materials import (
    reconcile_material_availability,
    seed_material,
    stage_material,
)
from sose.examples.construction.runtime import ORIGIN, build_runtime, seed_reference
from sose.examples.construction.scenarios import (
    procurement_delay_scenario,
    weather_delay_scenario,
)
from sose.persistence.memory import MemoryPersistence


def test_finite_procurement_delay_blocks_even_physically_available_material():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, quantity=10.0)
    context, engine = build_runtime(
        persistence,
        scenarios=(procurement_delay_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    reconcile_dependency(persistence, engine, entities=entities)
    seed_material(persistence, engine, backend, quantity=10.0)
    engine.advance_tick()
    backend.run_until(context.clock.now)

    assert context.scenarios.attribute(
        "construction.material.available", True
    ) is False
    assert reconcile_material_availability(
        persistence, engine, entities=entities
    ) is False
    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "waiting_material"

    for _ in range(6):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert context.scenarios.attribute(
        "construction.material.available", True
    ) is True
    assert reconcile_material_availability(
        persistence, engine, entities=entities
    ) is True
    assert stage_material(
        persistence, engine, backend, entities=entities
    )


def test_finite_weather_delay_defers_resource_acquisition_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, quantity=10.0)
    context, engine = build_runtime(
        persistence,
        scenarios=(weather_delay_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    reconcile_dependency(persistence, engine, entities=entities)
    seed_material(persistence, engine, backend, quantity=10.0)
    assert stage_material(
        persistence, engine, backend, entities=entities
    )

    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute(
        "construction.site.available", True
    ) is False
    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert persistence.resource_demands() == ()

    for _ in range(8):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert context.scenarios.attribute(
        "construction.site.available", True
    ) is True
    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True


def test_weather_outage_cancels_pending_or_raced_execution_capacity():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, quantity=10.0)
    context, engine = build_runtime(
        persistence,
        scenarios=(weather_delay_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    reconcile_dependency(persistence, engine, entities=entities)
    seed_material(persistence, engine, backend, quantity=10.0)
    assert stage_material(
        persistence, engine, backend, entities=entities
    )

    engine.resources.request(
        backend,
        resource_name="crew",
        request_id="weather-crew-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert any(
        demand.request_id == f"crew:{entities.activity_id}:initial"
        for demand in persistence.resource_demands()
    )

    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute(
        "construction.site.available", True
    ) is False

    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "weather-crew-blocker"
    )
    engine.resources.release(backend, blocker.reservation_id)
    backend.run_until(backend.now)

    # The previously queued request may have raced into a reservation.
    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    owned_ids = {
        f"crew:{entities.activity_id}:initial",
        f"equipment:{entities.activity_id}:initial",
    }
    assert not any(
        demand.request_id in owned_ids
        for demand in persistence.resource_demands()
    )
    assert not any(
        reservation.request_id in owned_ids
        for reservation in persistence.resource_reservations()
    )
