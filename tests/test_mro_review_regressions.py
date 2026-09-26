from sose.backends.simpy import SimPyBackend
from sose.examples.mro.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_cancel,
    reconcile_start,
    run_happy_path,
    seed_reference,
    seed_spare_parts,
)
from sose.persistence.memory import MemoryPersistence


def _released_runtime(quantity=1.0):
    persistence = MemoryPersistence()
    ids = seed_reference(persistence, quantity=quantity)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN.replace(hour=9))
    return persistence, ids, engine, backend


def test_completed_part_issue_is_reconciled_after_crash_even_when_stock_is_zero():
    persistence, ids, engine, backend = _released_runtime(quantity=1.0)
    seed_spare_parts(engine, backend, quantity=1.0)

    engine.stores.get(
        backend,
        store_name="spare_part_lots",
        request_id="consume-part-lot-1",
        requested_at=backend.now,
    )
    engine.containers.get(
        backend,
        container_name="spare_parts",
        request_id="consume-spare-part-1",
        amount=1.0,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert {state.name: state.level for state in persistence.container_states()} == {
        "spare_parts": 0.0
    }
    assert persistence.entity("part_demand", ids.part_demand_id).state == "open"

    _, restarted_engine = build_runtime(
        persistence,
        now=backend.now,
    )
    restarted_backend = SimPyBackend(origin=backend.now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(backend.now)

    assert reconcile_start(
        persistence,
        restarted_engine,
        restarted_backend,
        entities=ids,
        quantity=1.0,
    )
    assert persistence.entity("part_demand", ids.part_demand_id).state == "consumed"
    assert persistence.entity("work_order", ids.work_order_id).state == "in_progress"


def test_reconcile_start_on_closed_work_does_not_reacquire_capacity():
    persistence, ids = run_happy_path(quantity=1.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN.replace(hour=9))
    engine.rebuild_backend(backend)

    assert reconcile_start(
        persistence,
        engine,
        backend,
        entities=ids,
        quantity=1.0,
    ) is False
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.preemptive_resource_demands() == ()
    assert persistence.preemptive_resource_reservations() == ()


def test_planned_cancellation_invalidates_durable_release():
    persistence = MemoryPersistence()
    ids = seed_reference(persistence, quantity=1.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert persistence.scheduled_work()
    reconcile_cancel(persistence, engine, backend, entities=ids)

    assert persistence.entity("work_order", ids.work_order_id).state == "cancelled"
    assert persistence.scheduled_work() == ()
    backend.run_until(ORIGIN.replace(hour=9))
    assert persistence.entity("work_order", ids.work_order_id).state == "cancelled"
    assert persistence.scheduled_work() == ()


def test_cancelled_waiting_resource_discards_late_grants():
    persistence, ids, engine, backend = _released_runtime(quantity=1.0)
    seed_spare_parts(engine, backend, quantity=1.0)

    engine.resources.request(
        backend,
        resource_name="technician",
        request_id="technician-blocker",
        requested_at=backend.now,
        priority=1,
    )
    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id="bay-blocker",
        requested_at=backend.now,
        priority=1,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=1.0
    ) is False
    assert persistence.entity("work_order", ids.work_order_id).state == "waiting_resource"

    reconcile_cancel(persistence, engine, backend, entities=ids)
    assert persistence.entity("work_order", ids.work_order_id).state == "cancelled"

    technician_blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "technician-blocker"
    )
    bay_blocker = next(
        reservation
        for reservation in persistence.preemptive_resource_reservations()
        if reservation.request_id == "bay-blocker"
    )
    engine.resources.release(backend, technician_blocker.reservation_id)
    engine.preemptive_resources.release(backend, bay_blocker.reservation_id)
    backend.run_until(backend.now)

    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.preemptive_resource_demands() == ()
    assert persistence.preemptive_resource_reservations() == ()



def test_missing_lot_blocks_before_any_material_withdrawal():
    persistence, ids, engine, backend = _released_runtime(quantity=1.0)

    engine.containers.put(
        backend,
        container_name="spare_parts",
        request_id="seed-spare-parts",
        amount=1.0,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert reconcile_start(
        persistence,
        engine,
        backend,
        entities=ids,
        quantity=1.0,
    ) is False

    assert persistence.entity("work_order", ids.work_order_id).state == "waiting_material"
    assert persistence.store_get_requests() == ()
    assert not any(
        intent.request_id == "consume-spare-part-1"
        for intent in persistence.container_operation_intents()
    )
    assert {
        state.name: state.level for state in persistence.container_states()
    }["spare_parts"] == 1.0


def test_cancellation_is_rejected_after_part_issue_has_started():
    persistence, ids, engine, backend = _released_runtime(quantity=1.0)
    seed_spare_parts(engine, backend, quantity=1.0)

    from sose.examples.mro.simulation import reconcile_capacity, reconcile_part_issue

    assert reconcile_capacity(persistence, engine, backend, entities=ids)
    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=ids,
        quantity=1.0,
    )

    import pytest

    with pytest.raises(
        RuntimeError,
        match="cannot be cancelled after spare-part issue has started",
    ):
        reconcile_cancel(persistence, engine, backend, entities=ids)

    assert persistence.entity("work_order", ids.work_order_id).state == "released"
    assert persistence.entity("part_demand", ids.part_demand_id).state == "consumed"


def test_cancel_after_rebuild_removes_queued_demands_durably():
    persistence, ids, engine, backend = _released_runtime(quantity=1.0)
    seed_spare_parts(engine, backend, quantity=1.0)

    engine.resources.request(
        backend,
        resource_name="technician",
        request_id="technician-blocker-restart",
        requested_at=backend.now,
        priority=1,
    )
    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id="bay-blocker-restart",
        requested_at=backend.now,
        priority=1,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=1.0
    ) is False
    assert persistence.entity("work_order", ids.work_order_id).state == "waiting_resource"

    _, restarted_engine = build_runtime(persistence, now=backend.now)
    restarted_backend = SimPyBackend(origin=backend.now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(backend.now)

    reconcile_cancel(
        persistence,
        restarted_engine,
        restarted_backend,
        entities=ids,
    )

    technician_blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "technician-blocker-restart"
    )
    bay_blocker = next(
        reservation
        for reservation in persistence.preemptive_resource_reservations()
        if reservation.request_id == "bay-blocker-restart"
    )
    restarted_engine.resources.release(
        restarted_backend, technician_blocker.reservation_id
    )
    restarted_engine.preemptive_resources.release(
        restarted_backend, bay_blocker.reservation_id
    )
    restarted_backend.run_until(restarted_backend.now)

    assert persistence.entity("work_order", ids.work_order_id).state == "cancelled"
    assert persistence.resource_demands() == ()
    assert persistence.preemptive_resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.preemptive_resource_reservations() == ()
