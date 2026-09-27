from sose.backends.simpy import SimPyBackend
from sose.examples.construction.execution import (
    reconcile_dependency,
    request_execution_resources,
)
from sose.examples.construction.materials import seed_material, stage_material
from sose.examples.construction.runtime import ORIGIN, activity, build_runtime, seed_reference
from sose.examples.construction.scheduling import schedule_planned_start
from sose.persistence.memory import MemoryPersistence


def _prepare_pending_start(persistence):
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
    assert stage_material(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    due_at = schedule_planned_start(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert activity(persistence, entities.activity_id).state == "ready"
    assert len(persistence.scheduled_work()) == 1
    return entities, engine, backend, due_at


def _finish_start(persistence, entities, engine, backend, due_at):
    backend.run_until(due_at)
    assert activity(
        persistence,
        entities.activity_id,
    ).state == "waiting_resource"
    assert persistence.scheduled_work() == ()
    assert request_execution_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert activity(
        persistence,
        entities.activity_id,
    ).state == "executing"


def test_planned_start_is_durable_and_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend, c_due = _prepare_pending_start(
        continuous
    )
    _finish_start(
        continuous,
        c_entities,
        c_engine,
        c_backend,
        c_due,
    )

    restarted = MemoryPersistence()
    r_entities, _, r_backend_before, r_due = _prepare_pending_start(
        restarted
    )
    restart_at = r_backend_before.now
    _, rebuilt_engine = build_runtime(
        restarted,
        now=restart_at,
    )
    rebuilt_backend = SimPyBackend(origin=restart_at)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    rebuilt_backend.run_until(restart_at)

    assert activity(
        restarted,
        r_entities.activity_id,
    ).state == "ready"
    assert len(restarted.scheduled_work()) == 1

    _finish_start(
        restarted,
        r_entities,
        rebuilt_engine,
        rebuilt_backend,
        r_due,
    )

    assert activity(
        restarted,
        r_entities.activity_id,
    ) == activity(
        continuous,
        c_entities.activity_id,
    )
    assert restarted.scheduled_work() == continuous.scheduled_work() == ()
