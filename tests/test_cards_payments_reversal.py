from sose.backends.simpy import SimPyBackend
from sose.examples.cards_payments.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_authorization,
    reconcile_reversal,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_authorized_payment_can_reverse_before_capture():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_authorization(
        persistence, engine, backend, entities=entities, outcome="authorize"
    )
    assert reconcile_reversal(
        persistence, engine, entities=entities
    )
    assert persistence.entity("card_payment", entities.payment_id).state == "reversed"
    assert persistence.scheduled_work() == ()
