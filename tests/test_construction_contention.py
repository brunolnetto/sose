from sose.backends.simpy import SimPyBackend
from sose.examples.construction.execution import (
    reconcile_dependency,
    request_execution_resources,
)
from sose.examples.construction.materials import seed_material, stage_material
from sose.examples.construction.runtime import ORIGIN, build_runtime, seed_reference
from sose.persistence.memory import MemoryPersistence


def test_execution_waits_for_complete_crew_equipment_bundle():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, quantity=10.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    reconcile_dependency(persistence, engine, entities=entities)
    seed_material(persistence, engine, backend, quantity=10.0)
    assert stage_material(persistence, engine, backend, entities=entities)

    engine.resources.request(
        backend,
        resource_name="crew",
        request_id="crew-blocker",
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
    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "waiting_resource"
    assert any(
        demand.request_id == f"crew:{entities.activity_id}:initial"
        for demand in persistence.resource_demands()
    )
    assert not any(
        reservation.request_id == f"equipment:{entities.activity_id}:initial"
        for reservation in persistence.resource_reservations()
    )

    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "crew-blocker"
    )
    engine.resources.release(backend, blocker.reservation_id)
    backend.run_until(backend.now)

    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True
    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "executing"
