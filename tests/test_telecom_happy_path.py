from sose.examples.telecom.simulation import (
    run_happy_path,
    service_order_id,
    subscription_service_id,
    usage_record_id,
)


def test_postpaid_order_activates_service_and_records_usage():
    persistence, entities = run_happy_path()

    order = persistence.entity("telecom_product_order", entities.product_order_id)
    service_order = persistence.entity(
        "telecom_service_order",
        service_order_id(entities.product_order_id),
    )
    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    assert order is not None and order.state == "completed"
    assert service_order is not None and service_order.state == "completed"
    assert service is not None and service.state == "active"

    usage = persistence.entity(
        "telecom_usage_record",
        usage_record_id(service.id, 1),
    )
    assert usage is not None and usage.state == "committed"
    assert usage.attributes["quantity"] == 512.0
    assert usage.attributes["unit"] == "MB"
    assert usage.attributes["rated"] is False

    assert persistence.scheduled_work() == ()
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
