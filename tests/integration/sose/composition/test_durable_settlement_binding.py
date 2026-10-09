"""PC6 settlement destinations remain causal under value collisions and restart."""
from sose.composition import trading_company_customer as customer
from sose.composition.bindings import CustomerSettlementBinding, SettlementBindingService
from sose.domain.entity import Entity
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def test_equal_value_unrelated_payment_and_journal_cannot_hijack_completed_causal_path():
    result = customer.run_customer_demand_path()
    store = result.persistence

    with store.transaction() as uow:
        source_order = uow.get_entity("sales_order", result.o2c_order_id)
        payment = uow.get_entity("card_payment", result.payment_id)
        journal = uow.get_entity("journal_entry", result.journal_id)
        assert source_order and payment and journal
        for source, identity in (
            (source_order, "unrelated-sales-order"),
            (payment, "unrelated-card-payment"),
            (journal, "unrelated-journal"),
        ):
            uow.save_entity(Entity(
                id=identity, entity_type=source.entity_type,
                state=source.state, attributes=dict(source.attributes),
            ))
    SettlementBindingService(store).bind(CustomerSettlementBinding.create(
        order_id="unrelated-sales-order",
        payment_id="unrelated-card-payment",
        journal_id="unrelated-journal",
        amount=source_order.attributes["amount"],
        currency=source_order.attributes["currency"],
        correlation_id="independent-order",
    ))

    out_payment = customer.reconcile_invoiced_o2c_egress(store)
    out_accounting = customer.reconcile_settled_payment_egress(store)
    assert len(out_payment) == len(out_accounting) == 1
    assert out_payment[0].payload()["order_id"] == result.o2c_order_id
    assert out_payment[0].payload()["payment_id"] == result.payment_id
    assert out_accounting[0].payload()["journal_id"] == result.journal_id
    assert out_accounting[0].payload()["payment_id"] == result.payment_id


def test_sqlite_reopened_reference_recovers_same_causal_payment_journal_bindings(
    tmp_path, monkeypatch,
):
    path = tmp_path / "customer-binding.sqlite"
    opened = []

    def store_factory():
        store = SQLiteIncrementalPersistence(path)
        opened.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", store_factory)
    original = customer.run_customer_demand_path()
    opened[-1].close()

    with SQLiteIncrementalPersistence(path) as reopened:
        binding = SettlementBindingService(reopened).for_order(original.o2c_order_id)
        assert binding is not None
        assert binding.payment_id == original.payment_id
        assert binding.journal_id == original.journal_id
        assert binding.correlation_id == original.correlation_id
        payment_requests = customer.reconcile_invoiced_o2c_egress(reopened)
        journal_requests = customer.reconcile_settled_payment_egress(reopened)
        assert len(payment_requests) == len(journal_requests) == 1
        assert payment_requests[0].payload()["payment_id"] == binding.payment_id
        assert journal_requests[0].payload()["journal_id"] == binding.journal_id
