"""Two same-value PC6 customers must not alias fixed reference entity identities.

The legacy default fixtures remain byte-identical to the frozen v1 inputs.
"""
import pytest

from sose.composition.bindings import CustomerSettlementBinding, SettlementBindingService
from sose.examples.order_to_cash import simulation as o2c
from sose.examples.cards_payments import simulation as cards
from sose.examples.record_to_report import simulation as r2r
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_same_value_customer_fixture_instances_are_distinct_and_restartable(adapter, tmp_path):
    path = tmp_path / "multi-customer.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLiteIncrementalPersistence(path)
    try:
        instances = {}
        for key in ("customer-a", "customer-b"):
            order = o2c.seed_reference(store, amount=250.0, instance_key=key)
            payment = cards.seed_reference(store, amount=250.0, instance_key=key)
            journal = r2r.seed_reference(store, amount=250.0, instance_key=key)
            instances[key] = (order, payment, journal)
            _, engine = o2c.build_runtime(store)
            assert o2c.reconcile_credit(store, engine, entities=order)
            SettlementBindingService(store).bind(CustomerSettlementBinding.create(
                order_id=order.order_id, payment_id=payment.payment_id,
                journal_id=journal.journal_id, amount=250.0, currency="USD",
                correlation_id=f"pc6-{key}",
            ))
        for attribute, entity_type in (
            ("order_id", "sales_order"), ("payment_id", "card_payment"),
            ("journal_id", "journal_entry"), ("period_id", "accounting_period"),
        ):
            a, b = instances.values()
            a_value = next(getattr(item, attribute) for item in a if hasattr(item, attribute))
            b_value = next(getattr(item, attribute) for item in b if hasattr(item, attribute))
            assert a_value != b_value
            assert store.entity(entity_type, a_value) is not None
            assert store.entity(entity_type, b_value) is not None
        if adapter == "sqlite":
            store.close()
            store = SQLiteIncrementalPersistence(path)
        for key, (order, payment, journal) in instances.items():
            binding = SettlementBindingService(store).for_order(order.order_id)
            assert binding is not None
            assert binding.payment_id == payment.payment_id
            assert binding.journal_id == journal.journal_id
            assert store.entity("sales_order", order.order_id).state == "ordered"
        assert len({x.binding_id for x in (
            SettlementBindingService(store).for_order(v[0].order_id)
            for v in instances.values()
        )}) == 2
    finally:
        if adapter == "sqlite":
            store.close()


def test_legacy_fixture_default_identities_are_unchanged():
    from sose.core.identity import deterministic_id
    store = MemoryPersistence()
    order = o2c.seed_reference(store)
    payment = cards.seed_reference(store)
    journal = r2r.seed_reference(store)
    assert order.order_id == deterministic_id("entity", "sales_order", "o2c-reference", "order-1")
    assert payment.payment_id == deterministic_id("entity", "card_payment", "cards-reference", "payment-1")
    assert journal.period_id == deterministic_id("entity", "accounting_period", "r2r-reference", "period-2026-09")
    assert journal.journal_id == deterministic_id("entity", "journal_entry", "r2r-reference", journal.period_id, "journal-1")


@pytest.mark.parametrize("seed", [o2c.seed_reference, cards.seed_reference, r2r.seed_reference])
def test_empty_fixture_instance_key_is_rejected(seed):
    with pytest.raises(ValueError, match="instance_key"):
        seed(MemoryPersistence(), instance_key="")
