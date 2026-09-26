from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.p2p.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_consumption,
    reconcile_receiving_resources,
    reconcile_stocking,
    seed_happy_path,
)
from sose.persistence.memory import MemoryPersistence
from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


def _scenario() -> Scenario:
    return Scenario(
        name="p2p-restart-window",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=24),
        effects=(AttributeEffect("p2p.restart.window", True),),
    )


def _prepare(store: MemoryPersistence):
    ids = seed_happy_path(store, quantity=5.0)
    scenario = _scenario()
    context, engine = build_runtime(store, scenarios=(scenario,))
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.resources.request(
        backend,
        resource_name="receiving_dock",
        request_id="dock-blocker",
        requested_at=ORIGIN,
        priority=100,
    )
    backend.run_until(ORIGIN)

    engine.advance_tick()
    assert context.scenarios.attribute("p2p.restart.window", False) is True
    return ids, scenario, engine, backend


def _rebuild(store, scenario, *, now):
    context, engine = build_runtime(
        store,
        now=now,
        scenarios=(scenario,),
    )
    backend = SimPyBackend(origin=now)
    engine.rebuild_backend(backend)
    backend.run_until(now)
    return context, engine, backend


def _establish_blocked_boundary(store, ids, engine, backend):
    backend.run_until(ORIGIN + timedelta(hours=10))

    assert reconcile_receiving_resources(
        store,
        engine,
        backend,
        entities=ids,
    ) is False
    assert store.entity("receipt", ids.receipt_id).state == "pending"

    with pytest.raises(RuntimeError, match="inventory withdrawal is still pending"):
        reconcile_consumption(
            store,
            engine,
            backend,
            entities=ids,
            quantity=5.0,
        )

    assert [d.request_id for d in store.resource_demands()] == [
        f"receiving-dock:{ids.receipt_id}"
    ]
    assert [r.request_id for r in store.store_get_requests()] == [
        "consume-receipt-lot-1"
    ]
    assert [i.request_id for i in store.container_operation_intents()] == [
        "consume-demand-1"
    ]


def _finish(store, ids, engine, backend):
    blocker = next(
        reservation
        for reservation in store.resource_reservations()
        if reservation.request_id == "dock-blocker"
    )
    engine.resources.release(backend, blocker.reservation_id)
    backend.run_until(backend.now)

    assert reconcile_receiving_resources(
        store,
        engine,
        backend,
        entities=ids,
    ) is True
    assert store.entity("receipt", ids.receipt_id).state == "inspected"

    backend.run_until(ORIGIN + timedelta(hours=11))

    reconcile_stocking(
        store,
        engine,
        backend,
        entities=ids,
        quantity=5.0,
    )
    reconcile_consumption(
        store,
        engine,
        backend,
        entities=ids,
        quantity=5.0,
    )


def _snapshot(store, ids):
    return {
        "requisition": store.entity("requisition", ids.requisition_id),
        "purchase_order": store.entity("purchase_order", ids.purchase_order_id),
        "receipt": store.entity("receipt", ids.receipt_id),
        "material_demand": store.entity("material_demand", ids.material_demand_id),
        "events": store.events(),
        "scheduled_work": store.scheduled_work(),
        "position": store.simulation_position(),
        "scenario": store.scenario_state(),
        "resource_definitions": store.resource_definitions(),
        "resource_demands": store.resource_demands(),
        "resource_reservations": store.resource_reservations(),
        "resource_release_intents": store.resource_release_intents(),
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
    _establish_blocked_boundary(store, ids, engine, backend)
    _finish(store, ids, engine, backend)
    return store, ids


def _run_with_restarts():
    store = MemoryPersistence()
    ids, scenario, _, _ = _prepare(store)

    position = store.simulation_position()
    assert position is not None
    context2, _, backend2 = _rebuild(
        store,
        scenario,
        now=position.logical_time,
    )
    assert context2.scenarios.attribute("p2p.restart.window", False) is True

    backend2.run_until(ORIGIN + timedelta(hours=4))
    position = store.simulation_position()
    assert position is not None
    context3, engine3, backend3 = _rebuild(
        store,
        scenario,
        now=position.logical_time,
    )
    assert context3.scenarios.attribute("p2p.restart.window", False) is True

    _establish_blocked_boundary(store, ids, engine3, backend3)

    position = store.simulation_position()
    assert position is not None
    context4, engine4, backend4 = _rebuild(
        store,
        scenario,
        now=position.logical_time,
    )
    assert context4.scenarios.attribute("p2p.restart.window", False) is True

    dock = backend4.resource_snapshot("receiving_dock")
    assert dock.in_use == 1
    assert dock.queued == 1
    assert backend4.store_snapshot("received_lots").queued_gets == 1
    assert backend4.container_snapshot("inventory").queued_gets == 1

    _finish(store, ids, engine4, backend4)
    return store, ids


def test_p2p_multi_restart_equivalence_is_an_architectural_gate():
    continuous_store, continuous_ids = _run_continuous()
    restarted_store, restarted_ids = _run_with_restarts()

    assert _snapshot(restarted_store, restarted_ids) == _snapshot(
        continuous_store,
        continuous_ids,
    )

    assert restarted_store.entity(
        "requisition",
        restarted_ids.requisition_id,
    ).state == "ordered"
    assert restarted_store.entity(
        "purchase_order",
        restarted_ids.purchase_order_id,
    ).state == "closed"
    assert restarted_store.entity(
        "receipt",
        restarted_ids.receipt_id,
    ).state == "stocked"
    assert restarted_store.entity(
        "material_demand",
        restarted_ids.material_demand_id,
    ).state == "consumed"

    assert restarted_store.scheduled_work() == ()
    assert restarted_store.resource_demands() == ()
    assert restarted_store.resource_reservations() == ()
    assert restarted_store.resource_release_intents() == ()
    assert restarted_store.store_get_requests() == ()
    assert restarted_store.store_put_intents() == ()
    assert restarted_store.store_items() == ()
    assert restarted_store.container_operation_intents() == ()
    assert {
        state.name: state.level for state in restarted_store.container_states()
    } == {"inventory": 0.0}

    scenario = restarted_store.scenario_state()
    assert scenario is not None
    assert any(
        activation.scenario_name == "p2p-restart-window"
        for activation in scenario.activations
    )
