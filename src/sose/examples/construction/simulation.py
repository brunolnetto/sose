from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.persistence.memory import MemoryPersistence

from .execution import (
    complete_activity,
    finish_execution,
    reconcile_dependency,
    reconcile_inspection,
    record_measurement,
    request_execution_resources,
)
from .materials import (
    reconcile_material_availability,
    seed_material,
    stage_material,
)
from .runtime import (
    ConstructionEntities,
    ORIGIN,
    build_runtime,
    seed_reference,
)


def _prepare_ready_with_material(
    persistence: MemoryPersistence,
    *,
    quantity: float,
):
    entities = seed_reference(persistence, quantity=quantity)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    if not reconcile_dependency(
        persistence,
        engine,
        entities=entities,
    ):
        raise RuntimeError("construction dependency is not satisfied")

    seed_material(
        persistence,
        engine,
        backend,
        quantity=quantity,
    )
    reconcile_material_availability(
        persistence,
        engine,
        entities=entities,
    )
    if not stage_material(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("construction material was not staged")
    return entities, engine, backend


def run_happy_path(
    *,
    quantity: float = 10.0,
) -> tuple[MemoryPersistence, ConstructionEntities]:
    persistence = MemoryPersistence()
    entities, engine, backend = _prepare_ready_with_material(
        persistence,
        quantity=quantity,
    )

    if not request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("construction execution capacity unavailable")

    finish_execution(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    if not reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        outcome="pass",
    ):
        raise RuntimeError("construction inspection unavailable")

    record_measurement(
        persistence,
        engine,
        entities=entities,
        value=quantity,
    )
    complete_activity(
        persistence,
        engine,
        entities=entities,
    )
    return persistence, entities


def run_rework_path(
    *,
    quantity: float = 10.0,
) -> tuple[MemoryPersistence, ConstructionEntities]:
    persistence = MemoryPersistence()
    entities, engine, backend = _prepare_ready_with_material(
        persistence,
        quantity=quantity,
    )

    if not request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("construction execution capacity unavailable")
    finish_execution(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    if not reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        outcome="fail",
    ):
        raise RuntimeError("first construction inspection unavailable")

    if not request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
        cycle="rework-1",
    ):
        raise RuntimeError("construction rework capacity unavailable")
    finish_execution(
        persistence,
        engine,
        backend,
        entities=entities,
        cycle="rework-1",
    )
    if not reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=2,
        outcome="pass",
    ):
        raise RuntimeError("reinspection unavailable")

    record_measurement(
        persistence,
        engine,
        entities=entities,
        value=quantity,
    )
    complete_activity(
        persistence,
        engine,
        entities=entities,
    )
    return persistence, entities
