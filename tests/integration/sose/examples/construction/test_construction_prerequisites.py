import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.construction.execution import (
    complete_activity,
    complete_predecessor,
    reconcile_dependency,
    request_execution_resources,
)
from sose.examples.construction.materials import (
    reconcile_material_availability,
    seed_material,
    stage_material,
)
from sose.examples.construction.runtime import ORIGIN, build_runtime, seed_reference
from sose.persistence.memory import MemoryPersistence


def test_dependency_blocks_until_predecessor_is_durably_completed():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, predecessor_completed=False)
    _, engine = build_runtime(persistence)

    assert reconcile_dependency(
        persistence, engine, entities=entities
    ) is False
    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "blocked_dependency"

    complete_predecessor(
        persistence,
        engine,
        entities=entities,
    )
    assert reconcile_dependency(
        persistence, engine, entities=entities
    ) is True
    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "ready"


def test_material_shortage_precedes_any_execution_capacity_demand():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, quantity=10.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_dependency(
        persistence, engine, entities=entities
    )
    assert reconcile_material_availability(
        persistence, engine, entities=entities
    ) is False
    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "waiting_material"
    assert persistence.resource_demands() == ()

    seed_material(
        persistence,
        engine,
        backend,
        quantity=10.0,
    )
    assert reconcile_material_availability(
        persistence, engine, entities=entities
    ) is True
    assert stage_material(
        persistence, engine, backend, entities=entities
    ) is True

    current = persistence.entity(
        "construction_activity", entities.activity_id
    )
    assert current.attributes["material_staged"] is True
    assert persistence.store_items() == ()

    # Material staging is an orchestration prerequisite, not merely a state label.
    fresh = MemoryPersistence()
    fresh_entities = seed_reference(fresh, quantity=10.0)
    _, fresh_engine = build_runtime(fresh)
    fresh_backend = SimPyBackend(origin=ORIGIN)
    fresh_engine.rebuild_backend(fresh_backend)
    assert reconcile_dependency(
        fresh, fresh_engine, entities=fresh_entities
    )
    with pytest.raises(RuntimeError, match="before material staging"):
        request_execution_resources(
            fresh,
            fresh_engine,
            fresh_backend,
            entities=fresh_entities,
        )


def test_completion_requires_durable_measurement_evidence():
    from sose.examples.construction.execution import (
        finish_execution,
        reconcile_inspection,
        record_measurement,
    )

    persistence = MemoryPersistence()
    entities = seed_reference(persistence, quantity=5.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    reconcile_dependency(persistence, engine, entities=entities)
    seed_material(persistence, engine, backend, quantity=5.0)
    stage_material(persistence, engine, backend, entities=entities)
    assert request_execution_resources(
        persistence, engine, backend, entities=entities
    )
    finish_execution(persistence, engine, backend, entities=entities)
    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        outcome="pass",
    )

    with pytest.raises(
        RuntimeError,
        match="durable measurement evidence",
    ):
        complete_activity(persistence, engine, entities=entities)

    record_measurement(persistence, engine, entities=entities, value=5.0)
    complete_activity(persistence, engine, entities=entities)
    assert persistence.entity(
        "construction_activity", entities.activity_id
    ).state == "completed"


def test_predecessor_completion_requires_measurement_entity_evidence():
    from sose.examples.construction.runtime import measurement_id

    persistence = MemoryPersistence()
    entities = seed_reference(
        persistence,
        predecessor_completed=False,
    )
    _, engine = build_runtime(persistence)

    measurement = persistence.entity(
        "construction_measurement",
        measurement_id(entities.predecessor_id),
    )
    assert measurement is not None
    measurement.attributes["value"] = 0.0
    with persistence.transaction() as uow:
        uow.save_entity(measurement)

    with pytest.raises(
        RuntimeError,
        match="predecessor completion requires durable measurement evidence",
    ):
        complete_predecessor(
            persistence,
            engine,
            entities=entities,
        )

    assert persistence.entity(
        "construction_activity",
        entities.predecessor_id,
    ).state == "measured"
