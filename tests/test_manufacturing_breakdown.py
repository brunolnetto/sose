from sose.backends.simpy import SimPyBackend
from sose.examples.manufacturing.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_breakdown,
    reconcile_material_issue,
    reconcile_repair,
    reconcile_setup_resources,
    seed_happy_path,
    seed_material,
)
from sose.persistence.memory import MemoryPersistence


def _producing_runtime():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=5.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    seed_material(engine, backend, quantity=5.0)
    backend.run_until(ORIGIN.replace(hour=9))
    assert reconcile_setup_resources(
        persistence, engine, backend, entities=ids
    )
    reconcile_material_issue(
        persistence, engine, backend, entities=ids, quantity=5.0
    )
    return persistence, ids, engine, backend


def test_breakdown_preempts_machine_and_blocks_business_flow():
    persistence, ids, engine, backend = _producing_runtime()

    assert reconcile_breakdown(
        persistence, engine, backend, entities=ids
    ) is True

    assert persistence.entity("production_order", ids.production_order_id).state == "machine_down"
    assert persistence.entity("manufacturing_operation", ids.operation_id).state == "blocked"
    assert [r.request_id for r in persistence.preemptive_resource_reservations()] == [
        f"machine-repair:{ids.production_order_id}"
    ]
    result = persistence.resource_preemption_results()[0]
    assert result.displaced_request_id == f"machine:{ids.production_order_id}"
    assert result.preempting_request_id == f"machine-repair:{ids.production_order_id}"


def test_repair_reacquires_machine_before_resuming():
    persistence, ids, engine, backend = _producing_runtime()
    reconcile_breakdown(persistence, engine, backend, entities=ids)

    assert reconcile_repair(
        persistence, engine, backend, entities=ids
    ) is True

    assert persistence.entity("production_order", ids.production_order_id).state == "setup"
    assert persistence.entity("manufacturing_operation", ids.operation_id).state == "running"
    assert [r.request_id for r in persistence.preemptive_resource_reservations()] == [
        f"machine:{ids.production_order_id}"
    ]

    # The already committed material issue is reused rather than duplicated.
    reconcile_material_issue(
        persistence, engine, backend, entities=ids, quantity=5.0
    )
    assert persistence.entity("production_order", ids.production_order_id).state == "producing"
    assert [r.request_id for r in persistence.container_operation_results()].count(
        "issue-raw-material-1"
    ) == 1
