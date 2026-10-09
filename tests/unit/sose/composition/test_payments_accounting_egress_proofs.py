"""Payment→R2R causal proof tests for settled-only outbound messages."""
from dataclasses import replace
from datetime import datetime

import pytest

from sose.composition import trading_company_customer as customer
from sose.core.events import Command
from sose.domain.entity import Entity


def completed():
    result = customer.run_customer_demand_path()
    return result.persistence, result


@pytest.mark.parametrize("state", ["authorization_requested", "authorized", "captured", "settlement_pending"])
def test_accounting_entry_requires_durable_settled_payment(state):
    store, result = completed()
    with store.transaction() as uow:
        payment = uow.get_entity("card_payment", result.payment_id)
        payment.state = state
        uow.save_entity(payment)
    assert customer.reconcile_settled_payment_egress(store) == ()


def test_accounting_entry_needs_business_effect_not_transport_ack():
    store, result = completed()
    with store.transaction() as uow:
        request = next(
            d for d in uow.boundary_deliveries()
            if d.contract_name == "o2c.payment_requested"
        )
        accepted = uow.get_boundary_consumption(request.delivery_id)
        assert accepted is not None
        uow.save_command(Command(
            command_id=accepted.consumer_effect_id,
            name="composition.settle_customer_payment",
            entity_type="card_payment", entity_id=result.payment_id,
            due_at=datetime(2026, 9, 1),
        ))
    assert customer.reconcile_settled_payment_egress(store) == ()


def test_accounting_egress_rejects_mismatched_payment_amount():
    store, result = completed()
    with store.transaction() as uow:
        payment = uow.get_entity("card_payment", result.payment_id)
        payment.attributes["amount"] = -1
        uow.save_entity(payment)
    with pytest.raises(ValueError, match="settled card payment conflicts"):
        customer.reconcile_settled_payment_egress(store)


@pytest.mark.parametrize("mode", ["missing", "ambiguous"])
def test_accounting_egress_requires_unique_persisted_journal_binding(mode):
    store, result = completed()
    journal = store.entity("journal_entry", result.journal_id)
    assert journal is not None
    if mode == "missing":
        store._state.entities.pop(("journal_entry", journal.id))
    else:
        with store.transaction() as uow:
            uow.save_entity(Entity(
                id="other-journal", entity_type="journal_entry", state="drafted",
                attributes=dict(journal.attributes),
            ))
    if mode == "missing":
        with pytest.raises(RuntimeError, match="bound journal is missing"):
            customer.reconcile_settled_payment_egress(store)
    else:
        request = customer.reconcile_settled_payment_egress(store)
        assert len(request) == 1
        assert request[0].payload()["journal_id"] == result.journal_id


def test_accounting_egress_rejects_ack_without_receipt():
    store, _ = completed()
    with store.transaction() as uow:
        request = next(
            d for d in uow.boundary_deliveries()
            if d.contract_name == "o2c.payment_requested"
        )
        uow._working.boundary_consumptions.pop(request.delivery_id)
    with pytest.raises(RuntimeError, match="durable payment ACK receipt"):
        customer.reconcile_settled_payment_egress(store)


def test_accounting_egress_idempotent_bounded_and_correlation_scoped():
    store, result = completed()
    first = customer.reconcile_settled_payment_egress(store)
    assert len(first) == 1
    assert first[0].payload()["journal_id"] == result.journal_id
    assert customer.reconcile_settled_payment_egress(store, max_new_messages=0) == first
    assert customer.reconcile_settled_payment_egress(
        store, correlation_id="unrelated-order",
    ) == ()
    with pytest.raises(ValueError, match="max_new_messages"):
        customer.reconcile_settled_payment_egress(store, max_new_messages=-1)


def test_accounting_egress_rejects_conflicting_preexisting_identity():
    store, _ = completed()
    first = customer.reconcile_settled_payment_egress(store)[0]
    store._state.boundary_messages[first.message_id] = replace(first, correlation_id="wrong")
    with pytest.raises(ValueError, match="conflicts with source facts"):
        customer.reconcile_settled_payment_egress(store)
