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


def _inspection_runtime():
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
    return persistence, ids, engine, backend


def test_quality_hold_keeps_output_as_wip_not_finished_goods():
    persistence, ids, engine, backend = _inspection_runtime()

    reconcile_quality_hold(persistence, engine, entities=ids)

    assert persistence.entity("production_order", ids.production_order_id).state == "quality_hold"
    assert [item.item_id for item in persistence.store_items()] == ["wip-1"]
    levels = {state.name: state.level for state in persistence.container_states()}
    assert levels["finished_goods"] == 0.0
    assert [r.request_id for r in persistence.container_operation_results()] == [
        "seed-raw-material",
        "issue-raw-material-1",
    ]


def test_rework_does_not_issue_raw_material_twice_and_passes_second_inspection():
    persistence, ids, engine, backend = _inspection_runtime()
    reconcile_quality_hold(persistence, engine, entities=ids)

    reconcile_rework(persistence, engine, entities=ids)
    assert persistence.entity("production_order", ids.production_order_id).state == "producing"
    assert persistence.entity("manufacturing_operation", ids.operation_id).state == "running"

    # Rework uses the held WIP. The original raw-material issue is not repeated.
    assert [
        r.request_id
        for r in persistence.container_operation_results()
        if r.request_id == "issue-raw-material-1"
    ] == ["issue-raw-material-1"]

    reconcile_wip_output(
        persistence, engine, backend, entities=ids, quantity=5.0
    )
    reconcile_quality_pass(
        persistence, engine, backend, entities=ids, quantity=5.0
    )
    release_setup_resources(persistence, engine, backend, entities=ids)

    assert persistence.entity("production_order", ids.production_order_id).state == "completed"
    assert persistence.entity("manufacturing_operation", ids.operation_id).state == "done"
    assert persistence.store_items() == ()
    assert {state.name: state.level for state in persistence.container_states()} == {
        "finished_goods": 5.0,
        "raw_material": 0.0,
    }
