from sose.backends.simpy import SimPyBackend
from sose.examples.construction.execution import (
    complete_activity,
    finish_execution,
    reconcile_dependency,
    reconcile_inspection,
    record_measurement,
    request_execution_resources,
)
from sose.examples.construction.materials import seed_material, stage_material
from sose.examples.construction.runtime import (
    ORIGIN,
    build_runtime,
    inspection_id,
    measurement_id,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _prepare_rework_resource_wait(persistence):
    entities = seed_reference(persistence, quantity=10.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_dependency(
        persistence, engine, entities=entities
    )
    seed_material(persistence, engine, backend, quantity=10.0)
    assert stage_material(
        persistence, engine, backend, entities=entities
    )
    assert request_execution_resources(
        persistence, engine, backend, entities=entities
    )
    finish_execution(
        persistence, engine, backend, entities=entities
    )
    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        outcome="fail",
    )

    engine.resources.request(
        backend,
        resource_name="crew",
        request_id="rework-crew-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
        cycle="rework-1",
    ) is False
    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "waiting_resource"
    assert persistence.entity(
        "construction_inspection",
        inspection_id(entities.activity_id, 1),
    ).state == "failed"
    assert any(
        demand.request_id == f"crew:{entities.activity_id}:rework-1"
        for demand in persistence.resource_demands()
    )
    assert not any(
        reservation.request_id == f"equipment:{entities.activity_id}:rework-1"
        for reservation in persistence.resource_reservations()
    )
    return entities, engine, backend


def _finish_rework(persistence, entities, engine, backend):
    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "rework-crew-blocker"
    )
    engine.resources.release(backend, blocker.reservation_id)
    backend.run_until(backend.now)

    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
        cycle="rework-1",
    )
    finish_execution(
        persistence,
        engine,
        backend,
        entities=entities,
        cycle="rework-1",
    )
    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=2,
        outcome="pass",
    )
    record_measurement(
        persistence,
        engine,
        entities=entities,
        value=10.0,
    )
    complete_activity(
        persistence,
        engine,
        entities=entities,
    )


def _snapshot(persistence, entities):
    return {
        "predecessor": persistence.entity(
            "construction_activity", entities.predecessor_id
        ),
        "activity": persistence.entity(
            "construction_activity", entities.activity_id
        ),
        "inspection1": persistence.entity(
            "construction_inspection",
            inspection_id(entities.activity_id, 1),
        ),
        "inspection2": persistence.entity(
            "construction_inspection",
            inspection_id(entities.activity_id, 2),
        ),
        "measurement": persistence.entity(
            "construction_measurement",
            measurement_id(entities.activity_id),
        ),
        "events": persistence.events(),
        "resource_demands": persistence.resource_demands(),
        "resource_reservations": persistence.resource_reservations(),
        "release_intents": persistence.resource_release_intents(),
        "store_items": persistence.store_items(),
        "store_get_requests": persistence.store_get_requests(),
        "store_get_results": persistence.store_get_results(),
        "container_states": persistence.container_states(),
        "container_intents": persistence.container_operation_intents(),
        "container_results": persistence.container_operation_results(),
    }


def test_failed_inspection_pending_rework_capacity_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend = _prepare_rework_resource_wait(
        continuous
    )
    _finish_rework(
        continuous,
        c_entities,
        c_engine,
        c_backend,
    )

    restarted = MemoryPersistence()
    r_entities, _, r_backend_before = _prepare_rework_resource_wait(
        restarted
    )
    restart_at = r_backend_before.now
    _, rebuilt_engine = build_runtime(restarted, now=restart_at)
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    assert rebuilt_backend.resource_snapshot("crew").in_use == 1
    assert rebuilt_backend.resource_snapshot("crew").queued == 1

    _finish_rework(
        restarted,
        r_entities,
        rebuilt_engine,
        rebuilt_backend,
    )

    assert _snapshot(restarted, r_entities) == _snapshot(
        continuous, c_entities
    )
    assert restarted.entity(
        "construction_activity", r_entities.activity_id
    ).state == "completed"
    assert restarted.entity(
        "construction_inspection",
        inspection_id(r_entities.activity_id, 1),
    ).state == "failed"
    assert restarted.entity(
        "construction_inspection",
        inspection_id(r_entities.activity_id, 2),
    ).state == "passed"
