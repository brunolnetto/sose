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
    seed_happy_path,
    seed_material,
)
from sose.persistence.memory import MemoryPersistence


def test_rework_survives_restart_at_quality_hold():
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

    position = persistence.simulation_position()
    now = position.logical_time if position is not None else backend.now
    tick = position.logical_tick if position is not None else 0
    _, restarted_engine = build_runtime(persistence, now=now, tick=tick)
    restarted_backend = SimPyBackend(origin=now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(now)

    assert persistence.entity("production_order", ids.production_order_id).state == "quality_hold"
    assert [item.item_id for item in persistence.store_items()] == ["wip-1"]
    assert restarted_backend.preemptive_resource_snapshot("machine").in_use == 1
    assert restarted_backend.resource_snapshot("operator").in_use == 1

    reconcile_rework(persistence, restarted_engine, entities=ids)
    reconcile_wip_output(
        persistence,
        restarted_engine,
        restarted_backend,
        entities=ids,
        quantity=5.0,
    )
    reconcile_quality_pass(
        persistence,
        restarted_engine,
        restarted_backend,
        entities=ids,
        quantity=5.0,
    )

    assert persistence.entity("production_order", ids.production_order_id).state == "completed"
    assert {
        state.name: state.level for state in persistence.container_states()
    }["finished_goods"] == 5.0
