from sose.backends.simpy import SimPyBackend
from sose.examples.order_to_cash.scenarios import (
    credit_tightening_scenario,
    fulfillment_capacity_loss_scenario,
)
from sose.examples.order_to_cash.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_credit,
    reconcile_fulfillment,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_credit_tightening_holds_then_releases_order():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(credit_tightening_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_credit(
        persistence,
        engine,
        entities=entities,
    ) is False
    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None and order.state == "credit_hold"
    assert persistence.resource_demands() == ()

    for _ in range(8):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_credit(
        persistence,
        engine,
        entities=entities,
    ) is True
    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None and order.state == "ordered"


def test_fulfillment_outage_defers_capacity_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(fulfillment_capacity_loss_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    # Credit is independently available; fulfillment disruption is separate.
    assert reconcile_credit(
        persistence,
        engine,
        entities=entities,
    )

    engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()

    for _ in range(6):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True
    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None and order.state == "fulfilled"
