"""PC6 composition smoke gate against existing durable boundary implementation.

This test deliberately reuses the existing Trading Company execution paths:
no second message model, domain bus, inventory owner or test-only mechanism.
"""
from __future__ import annotations

from sose.composition.trading_company_customer import run_customer_demand_path
from sose.composition.trading_company_replenishment import run_replenishment_path
from sose.composition.model import DeliveryStatus


def _audited_boundary(result):
    with result.persistence.transaction() as uow:
        deliveries = uow.boundary_deliveries()
        messages = [uow.get_boundary_message(d.message_id) for d in deliveries]
        consumptions = [uow.get_boundary_consumption(d.delivery_id) for d in deliveries]
    assert deliveries
    assert all(d.status is DeliveryStatus.CONSUMED for d in deliveries)
    assert all(m is not None and m.payload_hash and m.message_id for m in messages)
    assert all(c is not None and c.consumer_effect_id for c in consumptions)
    assert len({d.delivery_id for d in deliveries}) == len(deliveries)
    assert len({m.message_id for m in messages}) == len(messages)
    assert {m.correlation_id for m in messages} == {result.correlation_id}
    assert len(set(result.message_ids)) == len(result.message_ids)
    assert {m.message_id for m in messages} == set(result.message_ids)
    assert {c.consumer_effect_id for c in consumptions} == set(result.effect_ids)
    return {m.contract_key for m in messages}


def test_pc6_two_composed_paths_preserve_durable_boundary_and_ownership():
    customer = run_customer_demand_path()
    replenishment = run_replenishment_path()

    customer_contracts = _audited_boundary(customer)
    replenishment_contracts = _audited_boundary(replenishment)
    assert "o2c.fulfillment_requested.v1" in customer_contracts
    assert "warehouse.inventory_reservation_requested.v1" in customer_contracts
    assert "warehouse.inventory_consumption_requested.v1" in customer_contracts
    assert "warehouse.replenishment_requested.v1" in replenishment_contracts
    assert "p2p.inventory_receipt_ready.v1" in replenishment_contracts
    assert "accounting.entry_requested.v1" in replenishment_contracts
    assert not any(
        item.entity_type == "warehouse_inventory_lot"
        for item in customer.persistence.entities()
    ), "composed Warehouse Fulfillment may not own authoritative stock"
