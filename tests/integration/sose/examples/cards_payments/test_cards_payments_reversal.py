from sose.backends.simpy import SimPyBackend
from sose.examples.cards_payments.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_authorization,
    reconcile_reversal,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


REFERENCE_OWNER_ID = "cards-payments-reference-organization"

def test_authorized_payment_can_reverse_before_capture():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=REFERENCE_OWNER_ID)
    _, engine = build_runtime(persistence, owner_id=REFERENCE_OWNER_ID)
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
