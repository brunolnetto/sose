from __future__ import annotations

from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.manufacturing.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_breakdown,
    reconcile_material_issue,
    reconcile_output,
    reconcile_repair,
    reconcile_setup_resources,
    release_setup_resources,
    seed_happy_path,
    seed_material,
)
from sose.persistence.memory import MemoryPersistence
from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


def _scenario() -> Scenario:
    return Scenario(
        name="manufacturing-restart-window",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=24),
        effects=(AttributeEffect("manufacturing.restart.window", True),),
    )


def _prepare(store: MemoryPersistence):
    ids = seed_happy_path(store, quantity=5.0)
    scenario = _scenario()
    context, engine = build_runtime(store, scenarios=(scenario,))
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.advance_tick()
    assert context.scenarios.attribute("manufacturing.restart.window", False) is True
    # Keep the ephemeral backend aligned with the durable logical clock so
    # continuous and reconstructed execution stamp operational completions
    # at the same semantic time.
    backend.run_until(context.clock.now)
    seed_material(engine, backend, quantity=5.0)
    return ids, scenario, engine, backend


def _rebuild(store, scenario):
    position = store.simulation_position()
    assert position is not None
    context, engine = build_runtime(
        store,
        now=position.logical_time,
        tick=position.logical_tick,
        scenarios=(scenario,),
    )
    backend = SimPyBackend(origin=position.logical_time)
    engine.rebuild_backend(backend)
    backend.run_until(position.logical_time)
    return context, engine, backend


def _establish_setup(store, ids, engine, backend):
    assert reconcile_setup_resources(store, engine, backend, entities=ids) is True
    assert store.entity("production_order", ids.production_order_id).state == "setup"
    assert [r.request_id for r in store.preemptive_resource_reservations()] == [
        f"machine:{ids.production_order_id}"
    ]
    assert [r.request_id for r in store.resource_reservations()] == [
        f"operator:{ids.production_order_id}"
    ]


def _establish_breakdown(store, ids, engine, backend):
    reconcile_material_issue(
        store, engine, backend, entities=ids, quantity=5.0
    )
    assert store.entity("production_order", ids.production_order_id).state == "producing"

    assert reconcile_breakdown(
        store, engine, backend, entities=ids
    ) is True
    assert store.entity("production_order", ids.production_order_id).state == "machine_down"
    assert store.entity("manufacturing_operation", ids.operation_id).state == "blocked"
    assert [r.request_id for r in store.preemptive_resource_reservations()] == [
        f"machine-repair:{ids.production_order_id}"
    ]
    assert len(store.resource_preemption_results()) == 1


def _finish(store, ids, engine, backend):
    assert reconcile_repair(
        store, engine, backend, entities=ids
    ) is True
    reconcile_material_issue(
        store, engine, backend, entities=ids, quantity=5.0
    )
    reconcile_output(
        store, engine, backend, entities=ids, quantity=5.0
    )
    release_setup_resources(
        store, engine, backend, entities=ids
    )


def _snapshot(store, ids):
    return {
        "order": store.entity("production_order", ids.production_order_id),
        "operation": store.entity("manufacturing_operation", ids.operation_id),
        "events": store.events(),
        "scheduled_work": store.scheduled_work(),
        "position": store.simulation_position(),
        "scenario": store.scenario_state(),
        "resource_definitions": store.resource_definitions(),
        "resource_demands": store.resource_demands(),
        "resource_reservations": store.resource_reservations(),
        "resource_release_intents": store.resource_release_intents(),
        "preemptive_definitions": store.preemptive_resource_definitions(),
        "preemptive_demands": store.preemptive_resource_demands(),
        "preemptive_reservations": store.preemptive_resource_reservations(),
        "preemptive_release_intents": store.preemptive_resource_release_intents(),
        "preemption_results": store.resource_preemption_results(),
        "store_definitions": store.store_definitions(),
        "store_items": store.store_items(),
        "store_put_intents": store.store_put_intents(),
        "store_get_requests": store.store_get_requests(),
        "store_get_results": store.store_get_results(),
        "container_definitions": store.container_definitions(),
        "container_states": store.container_states(),
        "container_intents": store.container_operation_intents(),
        "container_results": store.container_operation_results(),
    }


def _run_continuous():
    store = MemoryPersistence()
    ids, _, engine, backend = _prepare(store)
    _establish_setup(store, ids, engine, backend)
    _establish_breakdown(store, ids, engine, backend)
    _finish(store, ids, engine, backend)
    return store, ids


def _run_with_restarts():
    store = MemoryPersistence()
    ids, scenario, _, _ = _prepare(store)

    context2, engine2, backend2 = _rebuild(store, scenario)
    assert context2.scenarios.attribute("manufacturing.restart.window", False) is True
    _establish_setup(store, ids, engine2, backend2)

    context3, engine3, backend3 = _rebuild(store, scenario)
    assert backend3.preemptive_resource_snapshot("machine").in_use == 1
    assert backend3.resource_snapshot("operator").in_use == 1
    _establish_breakdown(store, ids, engine3, backend3)

    context4, engine4, backend4 = _rebuild(store, scenario)
    assert context4.scenarios.attribute("manufacturing.restart.window", False) is True
    assert backend4.preemptive_resource_snapshot("machine").in_use == 1
    _finish(store, ids, engine4, backend4)
    return store, ids


def test_manufacturing_multi_restart_equivalence_is_an_architectural_gate():
    continuous_store, continuous_ids = _run_continuous()
    restarted_store, restarted_ids = _run_with_restarts()

    assert _snapshot(restarted_store, restarted_ids) == _snapshot(
        continuous_store, continuous_ids
    )

    assert restarted_store.entity(
        "production_order", restarted_ids.production_order_id
    ).state == "completed"
    assert restarted_store.entity(
        "manufacturing_operation", restarted_ids.operation_id
    ).state == "done"

    assert restarted_store.scheduled_work() == ()
    assert restarted_store.resource_demands() == ()
    assert restarted_store.resource_reservations() == ()
    assert restarted_store.resource_release_intents() == ()
    assert restarted_store.preemptive_resource_demands() == ()
    assert restarted_store.preemptive_resource_reservations() == ()
    assert restarted_store.preemptive_resource_release_intents() == ()
    assert restarted_store.store_items() == ()
    assert restarted_store.store_put_intents() == ()
    assert restarted_store.store_get_requests() == ()
    assert restarted_store.container_operation_intents() == ()

    assert {
        state.name: state.level for state in restarted_store.container_states()
    } == {
        "finished_goods": 5.0,
        "raw_material": 0.0,
    }
    assert len(restarted_store.resource_preemption_results()) == 1
