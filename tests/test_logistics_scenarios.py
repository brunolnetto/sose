from sose.backends.simpy import SimPyBackend
from sose.examples.logistics.scenarios import (
    courier_capacity_loss_scenario,
    hub_congestion_scenario,
    weather_delay_scenario,
)
from sose.examples.logistics.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_pickup,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_logistics_scenario_builders_are_well_formed():
    hub = hub_congestion_scenario()
    courier = courier_capacity_loss_scenario()
    weather = weather_delay_scenario()

    assert hub.name == "logistics-hub-congestion"
    assert courier.name == "logistics-courier-capacity-loss"
    assert weather.name == "logistics-weather-delay"
    assert hub.duration.total_seconds() == 6 * 3600
    assert courier.duration.total_seconds() == 4 * 3600
    assert weather.duration.total_seconds() == 8 * 3600


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
