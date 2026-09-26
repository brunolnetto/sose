from sose.backends.simpy import SimPyBackend
from sose.examples.mro.scenarios import (
    asset_failure_scenario,
    spare_parts_disruption_scenario,
    technician_capacity_loss_scenario,
)
from sose.examples.mro.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_scenario_emergency,
    reconcile_start,
    seed_reference,
    seed_spare_parts,
)
from sose.persistence.memory import MemoryPersistence


def _scenario_runtime(scenario, quantity=1.0):
    persistence = MemoryPersistence()
    ids = seed_reference(persistence, quantity=quantity)
    context, engine = build_runtime(persistence, scenarios=(scenario,))
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    engine.advance_tick()
    backend.run_until(context.clock.now)
    return persistence, ids, context, engine, backend


def test_spare_parts_disruption_forces_waiting_material_even_with_stock():
    persistence, ids, _, engine, backend = _scenario_runtime(
        spare_parts_disruption_scenario()
    )
    seed_spare_parts(engine, backend, quantity=1.0)

    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=1.0
    ) is False
    assert persistence.entity("work_order", ids.work_order_id).state == "waiting_material"


def test_technician_capacity_loss_prevents_resource_claim():
    persistence, ids, _, engine, backend = _scenario_runtime(
        technician_capacity_loss_scenario()
    )
    seed_spare_parts(engine, backend, quantity=1.0)

    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=1.0
    ) is False
    assert persistence.entity("work_order", ids.work_order_id).state == "waiting_resource"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()


def test_asset_failure_scenario_routes_through_preemption():
    persistence, ids, context, engine, backend = _scenario_runtime(
        asset_failure_scenario()
    )
    seed_spare_parts(engine, backend, quantity=1.0)
    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=1.0
    )

    assert reconcile_scenario_emergency(
        persistence, engine, backend, entities=ids
    )
    assert persistence.entity("work_order", ids.work_order_id).state == "interrupted"
    assert len(persistence.resource_preemption_results()) == 1
    assert context.scenarios.attribute("mro.asset.emergency", False) is True


def test_finite_mro_scenario_expires_without_retriggering():
    persistence, _, context, engine, _ = _scenario_runtime(
        spare_parts_disruption_scenario()
    )
    assert context.scenarios.attribute("mro.spare_parts.available", True) is False

    for _ in range(5):
        engine.advance_tick()

    assert context.scenarios.attribute("mro.spare_parts.available", True) is True
    state = persistence.scenario_state()
    assert state is not None
    assert sum(
        decision.scenario_name == "mro-spare-parts-disruption"
        and decision.activated
        for decision in state.decisions
    ) == 1



def test_asset_failure_before_work_start_does_not_reserve_free_bay():
    persistence, ids, _, engine, backend = _scenario_runtime(
        asset_failure_scenario()
    )

    assert reconcile_scenario_emergency(
        persistence, engine, backend, entities=ids
    ) is False

    assert persistence.entity("work_order", ids.work_order_id).state == "released"
    assert persistence.preemptive_resource_demands() == ()
    assert persistence.preemptive_resource_reservations() == ()
    assert persistence.resource_preemption_results() == ()



def test_asset_failure_scenario_expiry_releases_emergency_and_resumes_work():
    persistence, ids, context, engine, backend = _scenario_runtime(
        asset_failure_scenario()
    )
    seed_spare_parts(engine, backend, quantity=1.0)
    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=1.0
    )
    assert reconcile_scenario_emergency(
        persistence, engine, backend, entities=ids
    )
    assert persistence.entity("work_order", ids.work_order_id).state == "interrupted"
    assert any(
        reservation.request_id.startswith(
            f"bay-emergency-scenario:{ids.work_order_id}:"
        )
        for reservation in persistence.preemptive_resource_reservations()
    )

    for _ in range(4):
        engine.advance_tick()
        backend.run_until(context.clock.now)

    assert context.scenarios.attribute("mro.asset.emergency", False) is False
    assert reconcile_scenario_emergency(
        persistence, engine, backend, entities=ids
    )

    assert persistence.entity("work_order", ids.work_order_id).state == "in_progress"
    assert [r.request_id for r in persistence.preemptive_resource_reservations()] == [
        f"bay:{ids.work_order_id}"
    ]



def test_expired_asset_failure_cleanup_is_idempotent_after_resume():
    persistence, ids, context, engine, backend = _scenario_runtime(
        asset_failure_scenario()
    )
    seed_spare_parts(engine, backend, quantity=1.0)
    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=1.0
    )
    assert reconcile_scenario_emergency(
        persistence, engine, backend, entities=ids
    )

    for _ in range(4):
        engine.advance_tick()
        backend.run_until(context.clock.now)

    assert reconcile_scenario_emergency(
        persistence, engine, backend, entities=ids
    )
    results_before = persistence.resource_preemption_results()
    events_before = persistence.events()

    assert reconcile_scenario_emergency(
        persistence, engine, backend, entities=ids
    ) is False

    assert persistence.entity("work_order", ids.work_order_id).state == "in_progress"
    assert persistence.resource_preemption_results() == results_before
    assert persistence.events() == events_before
    assert [r.request_id for r in persistence.preemptive_resource_reservations()] == [
        f"bay:{ids.work_order_id}"
    ]
