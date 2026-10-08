from __future__ import annotations

from sose.composition.model import DeliveryStatus
from sose.composition.trading_company_customer import run_customer_demand_path


def test_customer_demand_path_composes_domains_without_private_state_mutation() -> None:
    result = run_customer_demand_path()

    persistence = result.persistence
    assert persistence.entity("sales_order", result.o2c_order_id).state == "invoiced"
    assert (
        persistence.entity(
            "warehouse_fulfillment_order",
            result.fulfillment_order_id,
        ).state
        == "shipped"
    )
    assert persistence.entity("shipment", result.shipment_id).state == "delivered"
    assert persistence.entity("payment", result.payment_id).state == "settled"
    assert persistence.entity("journal_entry", result.journal_id).state == "posted"

    with persistence.transaction() as uow:
        deliveries = tuple(
            sorted(
                uow.boundary_deliveries(),
                key=lambda delivery: (
                    uow.get_boundary_message(delivery.message_id).produced_at,
                    delivery.delivery_id,
                ),
            )
        )
        messages = tuple(
            uow.get_boundary_message(delivery.message_id) for delivery in deliveries
        )

    assert [message.contract_key for message in messages] == [
        "o2c.fulfillment_requested.v1",
        "warehouse.dispatch_ready.v1",
        "logistics.delivery_completed.v1",
        "o2c.payment_requested.v1",
        "accounting.entry_requested.v1",
    ]
    assert all(delivery.status is DeliveryStatus.CONSUMED for delivery in deliveries)
    assert len({message.correlation_id for message in messages}) == 1

    assert messages[0].causation_id is None
    assert [message.causation_id for message in messages[1:]] == [
        message.message_id for message in messages[:-1]
    ]
    assert [message.produced_at for message in messages] == sorted(
        message.produced_at for message in messages
    )

    assert all(persistence.command(effect_id) is None for effect_id in result.effect_ids)


def test_customer_demand_path_preserves_boundary_and_domain_causality() -> None:
    result = run_customer_demand_path()
    persistence = result.persistence

    with persistence.transaction() as uow:
        messages = tuple(
            uow.get_boundary_message(message_id) for message_id in result.message_ids
        )

    message_ids = set(result.message_ids)
    event_causation = {
        event.causation_id
        for event in persistence.events()
        if event.causation_id is not None
    }

    assert messages[-1].source_domain == "cards_payments"
    assert messages[-1].destination_domain == "record_to_report"
    assert messages[-1].contract_key == "accounting.entry_requested.v1"

    # Boundary intents are durable causal links even though each domain keeps its own
    # transition/event vocabulary.
    assert set(result.effect_ids)
    assert all(message.message_id in message_ids for message in messages)
    assert any(event.correlation_id == result.correlation_id for event in persistence.events())
