from sose.backends.simpy import SimPyBackend
from sose.examples.p2p.scenarios import (
    demand_spike_scenario,
    receiving_congestion_scenario,
    supplier_delay_scenario,
)
from sose.examples.p2p.simulation import ORIGIN, build_runtime, seed_happy_path
from sose.persistence.memory import MemoryPersistence


def test_supplier_delay_is_external_context_and_survives_restart():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence)
    scenario = supplier_delay_scenario()
    context, engine = build_runtime(persistence, scenarios=(scenario,))

    engine.advance_tick()
    engine.advance_tick()
    engine.advance_tick()

    assert context.scenarios.attribute("p2p.supplier.delay", False) is True
    assert context.scenarios.attribute("p2p.supplier.lead_time_multiplier", 1.0) == 2.0

    purchase_order = persistence.entity("purchase_order", ids.purchase_order_id)
    assert purchase_order is not None
    assert purchase_order.state == "confirmed"

    command = context.commands.create(
        "mark_delayed",
        target=purchase_order,
        key=("p2p-supplier-delay", purchase_order.id, "mark-delayed"),
    )
    engine.dispatch(command)

    assert persistence.entity("purchase_order", ids.purchase_order_id).state == "delayed"

    saved = persistence.scenario_state()
    assert saved is not None
    assert any(
        activation.scenario_name == "p2p-supplier-delay"
        for activation in saved.activations
    )

    position = persistence.simulation_position()
    assert position is not None
    restarted_context, restarted_engine = build_runtime(
        persistence,
        now=position.logical_time,
        scenarios=(scenario,),
    )
    restarted_backend = SimPyBackend(origin=position.logical_time)
    restarted_engine.rebuild_backend(restarted_backend)

    assert restarted_context.scenarios.attribute("p2p.supplier.delay", False) is True
    assert (
        restarted_context.scenarios.attribute(
            "p2p.supplier.lead_time_multiplier",
            1.0,
        )
        == 2.0
    )


def test_demand_spike_and_receiving_congestion_are_policy_context_not_state_mutation():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence)
    scenarios = (demand_spike_scenario(), receiving_congestion_scenario())
    context, engine = build_runtime(persistence, scenarios=scenarios)

    before = {
        entity_type: persistence.entity(entity_type, entity_id).state
        for entity_type, entity_id in (
            ("requisition", ids.requisition_id),
            ("purchase_order", ids.purchase_order_id),
            ("receipt", ids.receipt_id),
            ("material_demand", ids.material_demand_id),
        )
    }

    engine.context.scenarios.on_tick()
    with persistence.transaction() as uow:
        uow.set_scenario_state(context.scenarios.snapshot_state())

    assert context.scenarios.attribute("p2p.demand.multiplier", 1.0) == 2.0
    assert context.scenarios.attribute("p2p.receiving.capacity_factor", 1.0) == 0.5

    after = {
        entity_type: persistence.entity(entity_type, entity_id).state
        for entity_type, entity_id in (
            ("requisition", ids.requisition_id),
            ("purchase_order", ids.purchase_order_id),
            ("receipt", ids.receipt_id),
            ("material_demand", ids.material_demand_id),
        )
    }
    assert after == before


def test_finite_supplier_delay_expires_without_retriggering():
    persistence = MemoryPersistence()
    seed_happy_path(persistence)
    scenario = supplier_delay_scenario()
    context, engine = build_runtime(persistence, scenarios=(scenario,))

    engine.advance_tick()
    assert context.scenarios.attribute("p2p.supplier.delay", False) is True

    for _ in range(6):
        engine.advance_tick()

    assert context.scenarios.attribute("p2p.supplier.delay", False) is False
    activations_after_expiry = tuple(context.scenarios.active_activations)

    engine.advance_tick()

    assert context.scenarios.attribute("p2p.supplier.delay", False) is False
    assert tuple(context.scenarios.active_activations) == activations_after_expiry
