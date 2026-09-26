from sose.backends.simpy import SimPyBackend
from sose.examples.manufacturing.scenarios import (
    machine_downtime_scenario,
    yield_degradation_scenario,
)
from sose.examples.manufacturing.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_material_issue,
    reconcile_output,
    reconcile_scenario_breakdown,
    reconcile_setup_resources,
    seed_happy_path,
    seed_material,
)
from sose.persistence.memory import MemoryPersistence


def test_yield_scenario_changes_finished_goods_without_changing_input_issue():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=10.0)
    context, engine = build_runtime(
        persistence, scenarios=(yield_degradation_scenario(),)
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    engine.advance_tick()
    seed_material(engine, backend, quantity=10.0)
    assert reconcile_setup_resources(persistence, engine, backend, entities=ids)
    reconcile_material_issue(
        persistence, engine, backend, entities=ids, quantity=10.0
    )
    reconcile_output(
        persistence, engine, backend, entities=ids, quantity=10.0
    )

    levels = {state.name: state.level for state in persistence.container_states()}
    assert levels["raw_material"] == 0.0
    assert levels["finished_goods"] == 8.0
    assert context.scenarios.attribute("manufacturing.yield.factor", 1.0) == 0.8


def test_machine_downtime_scenario_routes_through_durable_breakdown():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=5.0)
    context, engine = build_runtime(
        persistence, scenarios=(machine_downtime_scenario(),)
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    engine.advance_tick()
    seed_material(engine, backend, quantity=5.0)
    assert reconcile_setup_resources(persistence, engine, backend, entities=ids)
    reconcile_material_issue(
        persistence, engine, backend, entities=ids, quantity=5.0
    )

    assert reconcile_scenario_breakdown(
        persistence, engine, backend, entities=ids
    ) is True
    assert persistence.entity("production_order", ids.production_order_id).state == "machine_down"
    assert len(persistence.resource_preemption_results()) == 1


def test_finite_manufacturing_scenario_expires_without_retriggering():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=5.0)
    context, engine = build_runtime(
        persistence, scenarios=(yield_degradation_scenario(),)
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.advance_tick()
    assert context.scenarios.attribute("manufacturing.yield.factor", 1.0) == 0.8

    for _ in range(5):
        engine.advance_tick()

    assert context.scenarios.attribute("manufacturing.yield.factor", 1.0) == 1.0
    state = persistence.scenario_state()
    assert state is not None
    assert sum(
        decision.scenario_name == "manufacturing-yield-degradation"
        and decision.activated
        for decision in state.decisions
    ) == 1
