from sose.backends.simpy import SimPyBackend
from sose.examples.logistics.scenarios import courier_capacity_loss_scenario
from sose.examples.logistics.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_pickup,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_finite_courier_loss_routes_through_delay_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence, scenarios=(courier_capacity_loss_scenario(),)
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.advance_tick()
    backend.run_until(context.clock.now)

    assert context.scenarios.attribute("logistics.courier.available", True) is False
    assert reconcile_pickup(
        persistence, engine, backend, entities=entities
    ) is False
    assert persistence.entity(
        "shipment", entities.shipment_id
    ).state == "delayed_pickup"

    for _ in range(4):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert context.scenarios.attribute("logistics.courier.available", True) is True
    assert reconcile_pickup(
        persistence, engine, backend, entities=entities
    ) is True
    assert persistence.entity("shipment", entities.shipment_id).state == "picked_up"

    state = persistence.scenario_state()
    assert state is not None
    assert sum(
        decision.scenario_name == "logistics-courier-capacity-loss"
        and decision.activated
        for decision in state.decisions
    ) == 1
