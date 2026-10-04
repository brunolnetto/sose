from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.examples.mro.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_cancel,
    reconcile_capacity,
    reconcile_complete,
    reconcile_emergency_interrupt,
    reconcile_emergency_resume,
    reconcile_part_issue,
    reconcile_start,
    release_capacity,
    seed_reference,
    seed_spare_parts,
)
from sose.persistence.memory import MemoryPersistence


def _rebuild(store: MemoryPersistence):
    position = store.simulation_position()
    now = position.logical_time if position is not None else ORIGIN
    tick = position.logical_tick if position is not None else 0
    _, engine = build_runtime(store, now=now, tick=tick)
    backend = SimPyBackend(origin=now)
    engine.rebuild_backend(backend)
    backend.run_until(now)
    return engine, backend


def _prepare_with_parts(quantity=1.0):
    store = MemoryPersistence()
    ids = seed_reference(store, quantity=quantity)
    _, engine = build_runtime(store)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN.replace(hour=9))
    seed_spare_parts(engine, backend, quantity=quantity)
    return store, ids, engine, backend


def _prepare_without_parts(quantity=1.0):
    store = MemoryPersistence()
    ids = seed_reference(store, quantity=quantity)
    _, engine = build_runtime(store)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN.replace(hour=9))
    return store, ids, engine, backend


def _finish(store, ids, engine, backend, quantity=1.0):
    assert reconcile_start(
        store,
        engine,
        backend,
        entities=ids,
        quantity=quantity,
    )
    reconcile_complete(store, engine, entities=ids)
    release_capacity(store, engine, backend, entities=ids)


def _snapshot(store, ids):
    return {
        "work_order": store.entity("work_order", ids.work_order_id),
        "part_demand": store.entity("part_demand", ids.part_demand_id),
        "events": store.events(),
        "scheduled_work": store.scheduled_work(),
        "position": store.simulation_position(),
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


def test_mro_happy_path_restart_equivalence():
    continuous, continuous_ids, continuous_engine, continuous_backend = (
        _prepare_with_parts()
    )
    _finish(
        continuous,
        continuous_ids,
        continuous_engine,
        continuous_backend,
    )

    restarted, restarted_ids, _, _ = _prepare_with_parts()
    restarted_engine, restarted_backend = _rebuild(restarted)
    _finish(
        restarted,
        restarted_ids,
        restarted_engine,
        restarted_backend,
    )

    assert _snapshot(restarted, restarted_ids) == _snapshot(
        continuous, continuous_ids
    )
    assert restarted.entity("work_order", restarted_ids.work_order_id).state == "closed"


def test_mro_shortage_restart_equivalence():
    continuous, continuous_ids, continuous_engine, continuous_backend = (
        _prepare_without_parts()
    )
    assert reconcile_start(
        continuous,
        continuous_engine,
        continuous_backend,
        entities=continuous_ids,
        quantity=1.0,
    ) is False
    seed_spare_parts(continuous_engine, continuous_backend, quantity=1.0)
    _finish(
        continuous,
        continuous_ids,
        continuous_engine,
        continuous_backend,
    )

    restarted, restarted_ids, restarted_engine, restarted_backend = (
        _prepare_without_parts()
    )
    assert reconcile_start(
        restarted,
        restarted_engine,
        restarted_backend,
        entities=restarted_ids,
        quantity=1.0,
    ) is False
    assert restarted.entity(
        "work_order", restarted_ids.work_order_id
    ).state == "waiting_material"

    restarted_engine, restarted_backend = _rebuild(restarted)
    seed_spare_parts(restarted_engine, restarted_backend, quantity=1.0)
    _finish(
        restarted,
        restarted_ids,
        restarted_engine,
        restarted_backend,
    )

    assert _snapshot(restarted, restarted_ids) == _snapshot(
        continuous, continuous_ids
    )


def _prepare_interrupted():
    store, ids, engine, backend = _prepare_with_parts()
    assert reconcile_start(
        store, engine, backend, entities=ids, quantity=1.0
    )
    assert reconcile_emergency_interrupt(
        store, engine, backend, entities=ids
    )
    return store, ids, engine, backend


def _finish_interrupted(store, ids, engine, backend):
    assert reconcile_emergency_resume(
        store, engine, backend, entities=ids
    )
    reconcile_complete(store, engine, entities=ids)
    release_capacity(store, engine, backend, entities=ids)


def test_mro_emergency_restart_equivalence():
    continuous, continuous_ids, continuous_engine, continuous_backend = (
        _prepare_interrupted()
    )
    _finish_interrupted(
        continuous,
        continuous_ids,
        continuous_engine,
        continuous_backend,
    )

    restarted, restarted_ids, _, _ = _prepare_interrupted()
    assert restarted.entity(
        "work_order", restarted_ids.work_order_id
    ).state == "interrupted"

    restarted_engine, restarted_backend = _rebuild(restarted)
    assert restarted_backend.preemptive_resource_snapshot(
        "maintenance_bay"
    ).in_use == 1
    _finish_interrupted(
        restarted,
        restarted_ids,
        restarted_engine,
        restarted_backend,
    )

    assert _snapshot(restarted, restarted_ids) == _snapshot(
        continuous, continuous_ids
    )
    assert len(restarted.resource_preemption_results()) == 1


def test_mro_cancelled_state_is_restart_stable_and_inventory_neutral():
    store, ids, engine, backend = _prepare_with_parts()
    reconcile_cancel(store, engine, backend, entities=ids)
    before = _snapshot(store, ids)

    _rebuild(store)

    assert _snapshot(store, ids) == before
    assert store.entity("work_order", ids.work_order_id).state == "cancelled"
    assert store.entity("part_demand", ids.part_demand_id).state == "cancelled"
    assert store.store_get_results() == ()
    assert {state.name: state.level for state in store.container_states()} == {
        "spare_parts": 1.0
    }



def _prepare_waiting_resource():
    store, ids, engine, backend = _prepare_with_parts()

    engine.resources.request(
        backend,
        resource_name="technician",
        request_id="restart-technician-blocker",
        requested_at=backend.now,
        priority=1,
    )
    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id="restart-bay-blocker",
        requested_at=backend.now,
        priority=1,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert reconcile_start(
        store, engine, backend, entities=ids, quantity=1.0
    ) is False
    assert store.entity("work_order", ids.work_order_id).state == "waiting_resource"
    return store, ids, engine, backend


def _release_resource_blockers(store, engine, backend):
    technician = next(
        reservation
        for reservation in store.resource_reservations()
        if reservation.request_id == "restart-technician-blocker"
    )
    bay = next(
        reservation
        for reservation in store.preemptive_resource_reservations()
        if reservation.request_id == "restart-bay-blocker"
    )
    engine.resources.release(backend, technician.reservation_id)
    engine.preemptive_resources.release(backend, bay.reservation_id)
    backend.run_until(backend.now)


def test_mro_resource_queue_restart_equivalence():
    continuous, continuous_ids, continuous_engine, continuous_backend = (
        _prepare_waiting_resource()
    )
    _release_resource_blockers(
        continuous, continuous_engine, continuous_backend
    )
    _finish(
        continuous,
        continuous_ids,
        continuous_engine,
        continuous_backend,
    )

    restarted, restarted_ids, _, _ = _prepare_waiting_resource()
    restarted_engine, restarted_backend = _rebuild(restarted)

    assert restarted_backend.resource_snapshot("technician").queued == 1
    assert restarted_backend.preemptive_resource_snapshot(
        "maintenance_bay"
    ).queued == 1

    _release_resource_blockers(
        restarted, restarted_engine, restarted_backend
    )
    _finish(
        restarted,
        restarted_ids,
        restarted_engine,
        restarted_backend,
    )

    assert _snapshot(restarted, restarted_ids) == _snapshot(
        continuous, continuous_ids
    )


def _prepare_parts_consumed_before_start():
    store, ids, engine, backend = _prepare_with_parts()
    assert reconcile_capacity(store, engine, backend, entities=ids)
    assert reconcile_part_issue(
        store,
        engine,
        backend,
        entities=ids,
        quantity=1.0,
    )
    assert store.entity("work_order", ids.work_order_id).state == "released"
    assert store.entity("part_demand", ids.part_demand_id).state == "consumed"
    return store, ids, engine, backend


def test_mro_parts_consumed_boundary_restart_equivalence():
    continuous, continuous_ids, continuous_engine, continuous_backend = (
        _prepare_parts_consumed_before_start()
    )
    _finish(
        continuous,
        continuous_ids,
        continuous_engine,
        continuous_backend,
    )

    restarted, restarted_ids, _, _ = _prepare_parts_consumed_before_start()
    restarted_engine, restarted_backend = _rebuild(restarted)

    assert restarted_backend.resource_snapshot("technician").in_use == 1
    assert restarted_backend.preemptive_resource_snapshot(
        "maintenance_bay"
    ).in_use == 1
    assert restarted.entity("part_demand", restarted_ids.part_demand_id).state == "consumed"

    _finish(
        restarted,
        restarted_ids,
        restarted_engine,
        restarted_backend,
    )

    assert _snapshot(restarted, restarted_ids) == _snapshot(
        continuous, continuous_ids
    )


def _prepare_active_maintenance():
    store, ids, engine, backend = _prepare_with_parts()
    assert reconcile_start(
        store, engine, backend, entities=ids, quantity=1.0
    )
    assert store.entity("work_order", ids.work_order_id).state == "in_progress"
    return store, ids, engine, backend


def test_mro_active_maintenance_restart_equivalence():
    continuous, continuous_ids, continuous_engine, continuous_backend = (
        _prepare_active_maintenance()
    )
    reconcile_complete(continuous, continuous_engine, entities=continuous_ids)
    release_capacity(
        continuous,
        continuous_engine,
        continuous_backend,
        entities=continuous_ids,
    )

    restarted, restarted_ids, _, _ = _prepare_active_maintenance()
    restarted_engine, restarted_backend = _rebuild(restarted)

    assert restarted_backend.resource_snapshot("technician").in_use == 1
    assert restarted_backend.preemptive_resource_snapshot(
        "maintenance_bay"
    ).in_use == 1

    reconcile_complete(restarted, restarted_engine, entities=restarted_ids)
    release_capacity(
        restarted,
        restarted_engine,
        restarted_backend,
        entities=restarted_ids,
    )

    assert _snapshot(restarted, restarted_ids) == _snapshot(
        continuous, continuous_ids
    )


def _mark_completed(store, ids, engine):
    work_order = store.entity("work_order", ids.work_order_id)
    engine.dispatch(
        engine.context.commands.create(
            "complete",
            target=work_order,
            correlation_id=work_order.id,
            key=("mro-restart-boundary", work_order.id, "complete"),
        )
    )
    assert store.entity("work_order", ids.work_order_id).state == "completed"


def test_mro_completed_before_close_restart_equivalence():
    continuous, continuous_ids, continuous_engine, continuous_backend = (
        _prepare_active_maintenance()
    )
    _mark_completed(continuous, continuous_ids, continuous_engine)
    reconcile_complete(continuous, continuous_engine, entities=continuous_ids)
    release_capacity(
        continuous,
        continuous_engine,
        continuous_backend,
        entities=continuous_ids,
    )

    restarted, restarted_ids, engine_before_restart, _ = _prepare_active_maintenance()
    _mark_completed(restarted, restarted_ids, engine_before_restart)

    restarted_engine, restarted_backend = _rebuild(restarted)
    assert restarted.entity("work_order", restarted_ids.work_order_id).state == "completed"

    reconcile_complete(restarted, restarted_engine, entities=restarted_ids)
    release_capacity(
        restarted,
        restarted_engine,
        restarted_backend,
        entities=restarted_ids,
    )

    assert _snapshot(restarted, restarted_ids) == _snapshot(
        continuous, continuous_ids
    )



def test_mro_rebuilt_waiting_resource_can_be_cancelled_without_late_grants():
    store, ids, _, _ = _prepare_waiting_resource()
    engine, backend = _rebuild(store)

    reconcile_cancel(store, engine, backend, entities=ids)

    technician = next(
        reservation
        for reservation in store.resource_reservations()
        if reservation.request_id == "restart-technician-blocker"
    )
    bay = next(
        reservation
        for reservation in store.preemptive_resource_reservations()
        if reservation.request_id == "restart-bay-blocker"
    )
    engine.resources.release(backend, technician.reservation_id)
    engine.preemptive_resources.release(backend, bay.reservation_id)
    backend.run_until(backend.now)

    assert store.entity("work_order", ids.work_order_id).state == "cancelled"
    assert store.entity("part_demand", ids.part_demand_id).state == "cancelled"
    assert store.resource_demands() == ()
    assert store.preemptive_resource_demands() == ()
    assert store.resource_reservations() == ()
    assert store.preemptive_resource_reservations() == ()


def test_mro_cancellation_after_committed_part_issue_is_rejected():
    import pytest

    store, ids, engine, backend = _prepare_parts_consumed_before_start()

    with pytest.raises(
        RuntimeError,
        match="cannot be cancelled after spare-part issue has started",
    ):
        reconcile_cancel(store, engine, backend, entities=ids)

    assert store.entity("work_order", ids.work_order_id).state == "released"
    assert store.entity("part_demand", ids.part_demand_id).state == "consumed"
