from sose.backends.simpy import SimPyBackend
from sose.examples.manufacturing.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_setup_resources,
    seed_happy_path,
)
from sose.persistence.memory import MemoryPersistence


def test_setup_waits_for_machine_capacity():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN.replace(hour=9))

    engine.preemptive_resources.request(
        backend,
        resource_name="machine",
        request_id="machine-blocker",
        requested_at=backend.now,
        priority=50,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert reconcile_setup_resources(
        persistence, engine, backend, entities=ids
    ) is False
    assert persistence.entity("production_order", ids.production_order_id).state == "released"


def test_setup_requires_machine_and_operator_reservations():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN.replace(hour=9))

    assert reconcile_setup_resources(
        persistence, engine, backend, entities=ids
    ) is True
    assert persistence.entity("production_order", ids.production_order_id).state == "setup"
    assert persistence.entity("manufacturing_operation", ids.operation_id).state == "running"
    assert [r.request_id for r in persistence.preemptive_resource_reservations()] == [
        f"machine:{ids.production_order_id}"
    ]
    assert [r.request_id for r in persistence.resource_reservations()] == [
        f"operator:{ids.production_order_id}"
    ]
