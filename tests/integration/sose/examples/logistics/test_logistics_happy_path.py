from sose.examples.logistics.simulation import (
    delivery_attempt_id,
    flow_correlation_id,
    run_failed_retry_path,
    run_happy_path,
)


def test_logistics_happy_path_reaches_delivered_with_no_live_capacity():
    persistence, entities = run_happy_path()

    assert persistence.entity("shipment", entities.shipment_id).state == "delivered"
    assert persistence.entity("delivery_attempt", delivery_attempt_id(1)).state == "delivered"
    assert persistence.store_items() == ()
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.resource_release_intents() == ()
    assert persistence.scheduled_work() == ()
    assert {event.correlation_id for event in persistence.events()} == {
        flow_correlation_id()
    }


def test_failed_attempt_is_preserved_when_retry_succeeds():
    persistence, entities = run_failed_retry_path()

    assert persistence.entity("shipment", entities.shipment_id).state == "delivered"
    assert persistence.entity("delivery_attempt", delivery_attempt_id(1)).state == "failed"
    assert persistence.entity("delivery_attempt", delivery_attempt_id(2)).state == "delivered"
    assert persistence.scheduled_work() == ()
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
