from sose.backends.simpy import SimPyBackend
from sose.examples.cards_payments.scenarios import processor_outage_scenario
from sose.examples.cards_payments.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_authorization,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_finite_processor_outage_gates_authorization_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence, scenarios=(processor_outage_scenario(),)
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.advance_tick()
    backend.run_until(context.clock.now)

    assert context.scenarios.attribute("payments.processor.available", True) is False
    assert reconcile_authorization(
        persistence, engine, backend, entities=entities
    ) is False
    assert persistence.entity(
        "card_payment", entities.payment_id
    ).state == "authorization_requested"

    for _ in range(2):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert context.scenarios.attribute("payments.processor.available", True) is True
    assert reconcile_authorization(
        persistence, engine, backend, entities=entities
    ) is True
    assert persistence.entity("card_payment", entities.payment_id).state == "authorized"
