from __future__ import annotations

from sose.composition.model import DeliveryStatus
from sose.composition.trading_company_customer import run_customer_demand_path
from sose.examples.warehouse_fulfillment import simulation as fulfillment


def _entities_of_type(persistence, entity_type: str):
    return tuple(
        entity
        for entity in persistence.entities()
        if entity.entity_type == entity_type
    )


def test_customer_demand_path_composes_domains_with_warehouse_owned_stock() -> None:
    result = run_customer_demand_path()
    persistence = result.persistence

    assert persistence.entity("sales_order", result.o2c_order_id).state == "invoiced"
    order = persistence.entity(
        "warehouse_fulfillment_order",
        result.fulfillment_order_id,
    )
    assert order is not None and order.state == "shipped"
    assert persistence.entity("shipment", result.shipment_id).state == "delivered"
    assert persistence.entity("card_payment", result.payment_id).state == "settled"
    assert persistence.entity("journal_entry", result.journal_id).state == "posted"

    stock = persistence.entity(
        "warehouse_management_stock",
        result.warehouse_stock_id,
    )
    assert stock is not None
    assert stock.attributes["on_hand"] == 10.0
    assert stock.attributes["reserved"] == 0.0
    reservation = stock.attributes["external_reservations"][
        result.reservation_reference
    ]
    assert reservation == {
        "sku": fulfillment.PRIMARY_SKU,
        "quantity": 10.0,
        "consumed": True,
        "consumption_reference": result.consumption_reference,
    }

    # Composed mode must have exactly one authoritative stock truth: WM.
    assert _entities_of_type(persistence, "warehouse_inventory_lot") == ()
    allocations = tuple(
        persistence.entity("warehouse_allocation", allocation_id)
        for allocation_id in order.attributes["allocation_ids"]
    )
    assert len(allocations) == 1
    allocation = allocations[0]
    assert allocation is not None
    assert allocation.attributes["inventory_owner"] == "warehouse_management"
    assert allocation.attributes["stock_reference"] == result.warehouse_stock_id
    assert allocation.attributes["reservation_reference"] == result.reservation_reference
    assert "lot_id" not in allocation.attributes

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
        "warehouse.inventory_reservation_requested.v1",
        "warehouse.inventory_reserved.v1",
        "warehouse.inventory_consumption_requested.v1",
        "warehouse.dispatch_ready.v1",
        "logistics.delivery_completed.v1",
        "o2c.payment_requested.v1",
        "accounting.entry_requested.v1",
    ]
    assert all(delivery.status is DeliveryStatus.CONSUMED for delivery in deliveries)
    assert {message.correlation_id for message in messages} == {
        result.correlation_id
    }

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
    composed_event_types = {
        "warehouse_fulfillment_order",
        "warehouse_allocation",
        "warehouse_inventory_occurrence",
        "warehouse_management_stock",
        "shipment",
        "delivery_attempt",
        "card_payment",
        "journal_entry",
    }
    composed_events = tuple(
        event
        for event in persistence.events()
        if event.entity_type in composed_event_types
    )

    assert composed_events
    assert {event.correlation_id for event in composed_events} == {
        result.correlation_id
    }

    wm_events = tuple(
        event
        for event in composed_events
        if event.entity_type == "warehouse_management_stock"
    )
    assert [event.name for event in wm_events] == [
        "warehouse_management.inventory_reserved",
        "warehouse_management.inventory_consumed",
    ]

    assert messages[-1].source_domain == "cards_payments"
    assert messages[-1].destination_domain == "record_to_report"
    assert messages[-1].contract_key == "accounting.entry_requested.v1"

    assert set(result.effect_ids)
    assert all(message.message_id in message_ids for message in messages)
    assert any(
        event.correlation_id == result.correlation_id
        for event in persistence.events()
    )
