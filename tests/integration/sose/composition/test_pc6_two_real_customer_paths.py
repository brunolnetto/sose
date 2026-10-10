"""Real PC6 customer statecharts for two equal-value isolated orders.

Unlike boundary-only falsification, this executes actual WM, WF, Logistics,
O2C, Cards Payments and R2R transitions in a shared durable engine store.
"""
import pytest

from sose.composition.audit import audit_causal_history
from sose.composition.bindings import SettlementBindingService
from sose.composition.trading_company_customer import run_customer_demand_path
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_two_real_pc6_customer_paths_preserve_identities_and_business_truth(adapter, tmp_path):
    path = tmp_path / "pc6-multi-order.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLiteIncrementalPersistence(path)
    try:
        first = run_customer_demand_path(persistence=store, instance_key="pc6-order-a")
        second = run_customer_demand_path(persistence=store, instance_key="pc6-order-b")

        ids = (
            "o2c_order_id", "fulfillment_order_id", "shipment_id", "payment_id",
            "journal_id", "warehouse_stock_id",
        )
        assert all(getattr(first, item) != getattr(second, item) for item in ids)
        for result in (first, second):
            with store.transaction() as uow:
                ingress = uow.get_boundary_message(result.message_ids[0])
            assert ingress.contract_key == "o2c.fulfillment_requested.v2"
            assert ingress.payload()["shipment_id"] == result.shipment_id
            assert ingress.payload()["order_id"] == result.o2c_order_id
        assert len(set(first.message_ids + second.message_ids)) == 16
        assert len(set(first.effect_ids + second.effect_ids)) == 16
        assert len(store.business_effects()) == 8
        assert len({receipt.effect_id for receipt in store.business_effects()}) == 8

        def check():
            assert store.entity("sales_order", first.o2c_order_id).state == "invoiced"
            assert store.entity("sales_order", second.o2c_order_id).state == "invoiced"
            for result in (first, second):
                assert store.entity("shipment", result.shipment_id).state == "delivered"
                assert store.entity("card_payment", result.payment_id).state == "settled"
                assert store.entity("journal_entry", result.journal_id).state == "posted"
                assert SettlementBindingService(store).for_order(
                    result.o2c_order_id
                ).payment_id == result.payment_id
                assert SettlementBindingService(store).for_payment(
                    result.payment_id
                ).journal_id == result.journal_id
            report = audit_causal_history(store)
            assert report.message_count == 16
            assert report.applied_effects == 8
            assert report.pending_effects == 0
            assert all(store.command(effect.effect_id) is None for effect in store.business_effects())
            return report.semantic_digest

        before = check()
        if adapter == "sqlite":
            store.close()
            store = SQLiteIncrementalPersistence(path)
            assert check() == before
    finally:
        if adapter == "sqlite":
            store.close()


def test_default_pc6_reference_flow_retains_frozen_identity():
    from sose.examples.order_to_cash import simulation as o2c
    from sose.core.identity import deterministic_id

    result = run_customer_demand_path()
    assert result.o2c_order_id == deterministic_id(
        "entity", "sales_order", "o2c-reference", "order-1",
    )
    with result.persistence.transaction() as uow:
        ingress = uow.get_boundary_message(result.message_ids[0])
    assert ingress.contract_key == "o2c.fulfillment_requested.v1"
    assert "shipment_id" not in ingress.payload()
    assert result.correlation_id == deterministic_id(
        "trading-company-customer-demand", result.o2c_order_id,
    )


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_reusing_customer_instance_key_rejects_reseed_without_rewinding_truth(adapter, tmp_path):
    path = tmp_path / "resumption.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLiteIncrementalPersistence(path)
    try:
        result = run_customer_demand_path(persistence=store, instance_key="stable-owner")
        before = audit_causal_history(store)
        original = store.entity("sales_order", result.o2c_order_id)
        assert original.state == "invoiced"
        with pytest.raises(ValueError, match="already initialized"):
            run_customer_demand_path(persistence=store, instance_key="stable-owner")
        assert store.entity("sales_order", result.o2c_order_id) == original
        assert audit_causal_history(store) == before
    finally:
        if adapter == "sqlite":
            store.close()
