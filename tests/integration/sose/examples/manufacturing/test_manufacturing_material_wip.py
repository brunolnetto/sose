from sose.backends.simpy import SimPyBackend
from sose.examples.manufacturing.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_material_availability,
    run_happy_path,
    seed_happy_path,
    seed_material,
)
from sose.persistence.memory import MemoryPersistence


def test_material_shortage_is_explicit_business_state_until_replenishment():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=5.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN.replace(hour=9))

    reconcile_material_availability(
        persistence, engine, entities=ids
    )
    assert persistence.entity("production_order", ids.production_order_id).state == "waiting_material"

    seed_material(engine, backend, quantity=5.0)
    reconcile_material_availability(
        persistence, engine, entities=ids
    )
    assert persistence.entity("production_order", ids.production_order_id).state == "released"


def test_wip_is_consumed_before_finished_order_completes():
    persistence, ids = run_happy_path(quantity=8.0)

    assert persistence.entity("production_order", ids.production_order_id).state == "completed"
    assert persistence.store_items() == ()
    assert [r.request_id for r in persistence.store_get_results()] == [
        "issue-raw-lot-1",
        "consume-wip-1",
    ]
    assert {s.name: s.level for s in persistence.container_states()} == {
        "finished_goods": 8.0,
        "raw_material": 0.0,
    }
