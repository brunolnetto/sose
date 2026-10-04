from sose.backends.simpy import SimPyBackend
from sose.examples.construction.execution import (
    ensure_inspection,
    finish_execution,
    reconcile_dependency,
    reconcile_inspection,
    request_execution_resources,
)
from sose.examples.construction.materials import seed_material, stage_material
from sose.examples.construction.runtime import (
    ORIGIN,
    activity,
    build_runtime,
    dispatch,
    flow_correlation_id,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _ready_with_material(persistence):
    entities = seed_reference(persistence, quantity=10.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    assert reconcile_dependency(
        persistence,
        engine,
        entities=entities,
    )
    seed_material(
        persistence,
        engine,
        backend,
        quantity=10.0,
    )
    return entities, engine, backend


def test_material_staging_recovers_after_lot_get_commits_first():
    persistence = MemoryPersistence()
    entities, engine, backend = _ready_with_material(persistence)

    current = activity(persistence, entities.activity_id)
    lot_request = f"stage-lot:{current.id}"
    engine.stores.get(
        backend,
        store_name="material_lots",
        request_id=lot_request,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert any(
        result.request_id == lot_request
        for result in persistence.store_get_results()
    )
    assert persistence.store_items() == ()
    assert current.attributes.get("material_staged", False) is False

    assert stage_material(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    current = activity(persistence, entities.activity_id)
    assert current.attributes["material_staged"] is True
    assert current.attributes["staged_quantity"] == 10.0


def test_finish_cleanup_recovers_after_state_transition_commits():
    persistence = MemoryPersistence()
    entities, engine, backend = _ready_with_material(persistence)
    assert stage_material(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    current = activity(persistence, entities.activity_id)
    dispatch(
        engine,
        current,
        "finish_work",
        key=("construction", current.id, "initial", "finish-work"),
        correlation_id=flow_correlation_id(current.id),
    )

    assert activity(persistence, current.id).state == "inspection"
    assert {
        reservation.request_id
        for reservation in persistence.resource_reservations()
    } >= {
        f"crew:{current.id}:initial",
        f"equipment:{current.id}:initial",
    }

    finish_execution(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert persistence.resource_reservations() == ()


def test_inspector_cleanup_recovers_after_activity_accept_commits():
    persistence = MemoryPersistence()
    entities, engine, backend = _ready_with_material(persistence)
    assert stage_material(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    finish_execution(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    occurrence = ensure_inspection(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
    )
    request_id = f"inspector:{occurrence.id}"
    engine.resources.request(
        backend,
        resource_name="inspector",
        request_id=request_id,
        requested_at=backend.now,
        priority=100,
    )
    backend.run_until(backend.now)

    correlation_id = flow_correlation_id(entities.activity_id)
    dispatch(
        engine,
        occurrence,
        "begin",
        key=("construction-inspection", occurrence.id, "begin"),
        correlation_id=correlation_id,
    )
    occurrence = persistence.entity(
        "construction_inspection",
        occurrence.id,
    )
    assert occurrence is not None
    dispatch(
        engine,
        occurrence,
        "pass_inspection",
        key=("construction-inspection", occurrence.id, "pass_inspection"),
        correlation_id=correlation_id,
    )

    current = activity(persistence, entities.activity_id)
    dispatch(
        engine,
        current,
        "accept",
        key=("construction", current.id, occurrence.id, "accept"),
        correlation_id=correlation_id,
    )

    assert activity(persistence, current.id).state == "measured"
    assert any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )

    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        outcome="pass",
    )
    assert not any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )
