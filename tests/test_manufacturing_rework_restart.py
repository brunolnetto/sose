from sose.backends.simpy import SimPyBackend
from sose.examples.manufacturing.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_material_issue,
    reconcile_quality_hold,
    reconcile_quality_pass,
    reconcile_rework,
    reconcile_setup_resources,
    reconcile_wip_output,
    release_setup_resources,
    seed_happy_path,
    seed_material,
)
from sose.persistence.memory import MemoryPersistence


def _prepare_quality_hold():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=5.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    seed_material(engine, backend, quantity=5.0)
    backend.run_until(ORIGIN.replace(hour=9))
    assert reconcile_setup_resources(persistence, engine, backend, entities=ids)
    reconcile_material_issue(
        persistence, engine, backend, entities=ids, quantity=5.0
    )
    reconcile_wip_output(
        persistence, engine, backend, entities=ids, quantity=5.0
    )
    reconcile_quality_hold(persistence, engine, entities=ids)
    return persistence, ids, engine, backend


def _finish_rework(persistence, ids, engine, backend):
    reconcile_rework(persistence, engine, entities=ids)
    reconcile_wip_output(
        persistence,
        engine,
        backend,
        entities=ids,
        quantity=5.0,
    )
    reconcile_quality_pass(
        persistence,
        engine,
        backend,
        entities=ids,
        quantity=5.0,
    )
    release_setup_resources(
        persistence,
        engine,
        backend,
        entities=ids,
    )


def _snapshot(persistence, ids):
    return {
        "order": persistence.entity("production_order", ids.production_order_id),
        "operation": persistence.entity("manufacturing_operation", ids.operation_id),
        "events": persistence.events(),
        "store_items": persistence.store_items(),
        "store_get_results": persistence.store_get_results(),
        "container_states": persistence.container_states(),
        "container_results": persistence.container_operation_results(),
        "resource_demands": persistence.resource_demands(),
        "resource_reservations": persistence.resource_reservations(),
        "preemptive_demands": persistence.preemptive_resource_demands(),
        "preemptive_reservations": persistence.preemptive_resource_reservations(),
    }


def test_quality_rework_restart_is_semantically_equivalent():
    continuous, continuous_ids, continuous_engine, continuous_backend = (
        _prepare_quality_hold()
    )
    _finish_rework(
        continuous,
        continuous_ids,
        continuous_engine,
        continuous_backend,
    )

    restarted, restarted_ids, _, backend = _prepare_quality_hold()
    position = restarted.simulation_position()
    now = position.logical_time if position is not None else backend.now
    tick = position.logical_tick if position is not None else 0
    _, restarted_engine = build_runtime(restarted, now=now, tick=tick)
    restarted_backend = SimPyBackend(origin=now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(now)

    assert restarted.entity(
        "production_order", restarted_ids.production_order_id
    ).state == "quality_hold"
    assert [item.item_id for item in restarted.store_items()] == ["wip-1"]
    assert restarted_backend.preemptive_resource_snapshot("machine").in_use == 1
    assert restarted_backend.resource_snapshot("operator").in_use == 1

    _finish_rework(
        restarted,
        restarted_ids,
        restarted_engine,
        restarted_backend,
    )

    assert _snapshot(restarted, restarted_ids) == _snapshot(
        continuous, continuous_ids
    )
    assert restarted.entity(
        "production_order", restarted_ids.production_order_id
    ).state == "completed"
    assert {
        state.name: state.level for state in restarted.container_states()
    } == {
        "finished_goods": 5.0,
        "raw_material": 0.0,
    }
