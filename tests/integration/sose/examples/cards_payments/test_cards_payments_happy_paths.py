from sose.examples.cards_payments.simulation import (
    dispute_entity_id,
    run_dispute_path,
    run_happy_path,
    run_refund_path,
    run_retry_path,
)


REFERENCE_OWNER_ID = "cards-payments-reference-organization"

def test_cards_happy_path_reaches_settled():
    persistence, entities = run_happy_path(owner_id=REFERENCE_OWNER_ID)

    assert persistence.entity("card_payment", entities.payment_id).state == "settled"
    assert persistence.scheduled_work() == ()
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.resource_release_intents() == ()


def test_settlement_retry_eventually_settles_without_losing_history():
    persistence, entities = run_retry_path(owner_id=REFERENCE_OWNER_ID)

    assert persistence.entity("card_payment", entities.payment_id).state == "settled"
    names = [event.name for event in persistence.events()]
    assert "entity.state_transition" in names
    assert persistence.scheduled_work() == ()


def test_refund_is_post_settlement_terminal_fact():
    persistence, entities = run_refund_path(owner_id=REFERENCE_OWNER_ID)

    assert persistence.entity("card_payment", entities.payment_id).state == "refunded"
    assert persistence.resource_reservations() == ()


def test_dispute_is_independent_from_settled_payment():
    persistence, entities = run_dispute_path(owner_id=REFERENCE_OWNER_ID, outcome="merchant")

    assert persistence.entity("card_payment", entities.payment_id).state == "settled"
    assert persistence.entity(
        "payment_dispute", dispute_entity_id()
    ).state == "merchant_won"
    assert persistence.scheduled_work() == ()
    assert persistence.resource_reservations() == ()
