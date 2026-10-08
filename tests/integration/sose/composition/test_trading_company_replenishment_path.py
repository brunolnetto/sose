from __future__ import annotations

from sose.composition.model import DeliveryStatus
from sose.composition.trading_company_replenishment import run_replenishment_path


def test_replenishment_path_keeps_warehouse_management_as_stock_owner() -> None:
    result = run_replenishment_path(quantity=5.0)
    store = result.persistence

    stock = store.entity("warehouse_management_stock", result.stock_id)
    receipt = store.entity("receipt", result.receipt_id)
    journal = store.entity("journal_entry", result.journal_id)

    assert stock is not None
    assert stock.attributes["on_hand"] == 7.0
    assert stock.attributes["sku"] == "bearing-6204"
    assert stock.attributes["external_receipts"] == {result.receipt_id: 5.0}
    assert receipt is not None and receipt.state == "stocked"
    assert journal is not None and journal.state == "posted"

    # P2P staging inventory is handed off after WM durably accepts the receipt, so
    # the composed final state has one authoritative stock truth.
    assert {
        state.name: state.level for state in store.container_states()
    }["inventory"] == 0.0

    with store.transaction() as uow:
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
        "warehouse.replenishment_requested.v1",
        "p2p.inventory_receipt_ready.v1",
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
    assert messages[-1].source_domain == "procure_to_pay"
    assert messages[-1].source_identity == result.receipt_id


def test_replenishment_receipt_replay_does_not_duplicate_stock() -> None:
    result = run_replenishment_path(quantity=5.0)
    store = result.persistence

    stock = store.entity("warehouse_management_stock", result.stock_id)
    assert stock.attributes["on_hand"] == 7.0

    # The path deliberately replays the receipt delivery once after successful
    # handoff. Durable consumption + WM receipt identity must converge.
    assert result.receipt_replay_was_idempotent is True
    stock_after = store.entity("warehouse_management_stock", result.stock_id)
    assert stock_after.attributes["on_hand"] == 7.0


def test_replenishment_domain_events_share_composition_correlation() -> None:
    result = run_replenishment_path(quantity=5.0)
    store = result.persistence

    relevant_types = {
        "requisition",
        "purchase_order",
        "receipt",
        "material_demand",
        "warehouse_management_stock",
        "journal_entry",
    }
    relevant = tuple(
        event for event in store.events() if event.entity_type in relevant_types
    )
    assert relevant
    assert {event.correlation_id for event in relevant} == {
        result.correlation_id
    }
