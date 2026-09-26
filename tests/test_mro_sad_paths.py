from sose.backends.simpy import SimPyBackend
from sose.examples.mro.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_cancel,
    reconcile_start,
    seed_reference,
    seed_spare_parts,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(quantity=1.0):
    persistence = MemoryPersistence()
    ids = seed_reference(persistence, quantity=quantity)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN.replace(hour=9))
    return persistence, ids, engine, backend


def test_spare_part_shortage_is_visible_before_capacity_is_acquired():
    persistence, ids, engine, backend = _runtime(quantity=2.0)

    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=2.0
    ) is False

    assert persistence.entity("work_order", ids.work_order_id).state == "waiting_material"
    assert persistence.entity("part_demand", ids.part_demand_id).state == "waiting_inventory"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.preemptive_resource_demands() == ()
    assert persistence.preemptive_resource_reservations() == ()
    assert persistence.store_get_requests() == ()
    assert persistence.container_operation_intents() == ()


def test_replenishment_resumes_waiting_material_and_starts_work():
    persistence, ids, engine, backend = _runtime(quantity=2.0)
    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=2.0
    ) is False

    seed_spare_parts(engine, backend, quantity=2.0)
    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=2.0
    ) is True

    assert persistence.entity("work_order", ids.work_order_id).state == "in_progress"
    assert persistence.entity("part_demand", ids.part_demand_id).state == "consumed"


def test_technician_contention_keeps_work_order_waiting_resource():
    persistence, ids, engine, backend = _runtime()
    seed_spare_parts(engine, backend, quantity=1.0)

    engine.resources.request(
        backend,
        resource_name="technician",
        request_id="technician-blocker",
        requested_at=backend.now,
        priority=10,
    )
    backend.run_until(backend.now)

    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=1.0
    ) is False
    assert persistence.entity("work_order", ids.work_order_id).state == "waiting_resource"
    assert any(
        demand.request_id == f"technician:{ids.work_order_id}"
        for demand in persistence.resource_demands()
    )


def test_cancellation_releases_capacity_and_avoids_part_consumption():
    persistence, ids, engine, backend = _runtime()
    seed_spare_parts(engine, backend, quantity=1.0)

    # Acquire capacity directly but cancel before part issue / start.
    from sose.examples.mro.simulation import reconcile_capacity

    assert reconcile_capacity(persistence, engine, backend, entities=ids)
    reconcile_cancel(persistence, engine, backend, entities=ids)

    assert persistence.entity("work_order", ids.work_order_id).state == "cancelled"
    assert persistence.entity("part_demand", ids.part_demand_id).state == "cancelled"
    assert persistence.resource_reservations() == ()
    assert persistence.preemptive_resource_reservations() == ()
    assert persistence.store_get_results() == ()
    assert {
        state.name: state.level for state in persistence.container_states()
    }["spare_parts"] == 1.0
