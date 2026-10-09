"""Prove invoice / receivable / card binding rather than transport ACK alone."""
from dataclasses import replace
from datetime import datetime

import pytest

from sose.composition import trading_company_customer as customer
from sose.core.events import Command
from sose.domain.entity import Entity


def completed_fixture():
    result = customer.run_customer_demand_path()
    return result.persistence, result


def inbound_receipt(store):
    with store.transaction() as uow:
        delivery = next(
            d for d in uow.boundary_deliveries()
            if d.contract_name == "logistics.delivery_completed"
        )
        return uow.get_boundary_consumption(delivery.delivery_id)


@pytest.mark.parametrize(
    "variant",
    [
        "pending_command",
        "not_invoiced",
        "no_receivable",
        "wrong_receivable_amount",
        "wrong_receivable_order",
    ],
)
def test_payment_egress_fails_closed_without_applied_invoicing(variant):
    store, reference = completed_fixture()
    receipt = inbound_receipt(store)
    if variant == "pending_command":
        with store.transaction() as uow:
            uow.save_command(Command(
                command_id=receipt.consumer_effect_id, name="composition.complete_external_fulfillment",
                entity_type="sales_order", entity_id=reference.o2c_order_id,
                due_at=datetime(2026, 9, 1),
            ))
    elif variant == "not_invoiced":
        with store.transaction() as uow:
            order = uow.get_entity("sales_order", reference.o2c_order_id)
            order.state = "shipped"
            uow.save_entity(order)
    else:
        from sose.examples.order_to_cash import simulation as o2c
        rid = o2c.receivable_id(reference.o2c_order_id)
        if variant == "no_receivable":
            store._state.entities.pop(("receivable", rid))
        else:
            with store.transaction() as uow:
                value = uow.get_entity("receivable", rid)
                if variant == "wrong_receivable_amount":
                    value.attributes["amount"] += 1
                else:
                    value.attributes["order_id"] = "other-order"
                uow.save_entity(value)

    if variant in {"wrong_receivable_amount", "wrong_receivable_order"}:
        with pytest.raises(ValueError, match="receivable does not certify"):
            customer.reconcile_invoiced_o2c_egress(store)
    else:
        assert customer.reconcile_invoiced_o2c_egress(store) == ()


@pytest.mark.parametrize("variant", ["missing_payment", "ambiguous_payment"])
def test_payment_egress_does_not_guess_binding_across_entities(variant):
    store, reference = completed_fixture()
    payment = store.entity("card_payment", reference.payment_id)
    assert payment is not None
    if variant == "missing_payment":
        store._state.entities.pop(("card_payment", payment.id))
    else:
        with store.transaction() as uow:
            uow.save_entity(Entity(
                id="another-payment", entity_type="card_payment",
                state=payment.state, attributes=dict(payment.attributes),
            ))
    with pytest.raises(RuntimeError, match="one durable payment binding"):
        customer.reconcile_invoiced_o2c_egress(store)


def test_receipt_deleted_is_not_proof_of_applied_o2c():
    store, _ = completed_fixture()
    with store.transaction() as uow:
        delivery = next(
            d for d in uow.boundary_deliveries()
            if d.contract_name == "logistics.delivery_completed"
        )
        uow._working.boundary_consumptions.pop(delivery.delivery_id)
    with pytest.raises(RuntimeError, match="missing consumption evidence"):
        customer.reconcile_invoiced_o2c_egress(store)


def test_completed_invoice_request_is_stable_under_duplicate_reconciliation():
    store, reference = completed_fixture()
    original = customer.reconcile_invoiced_o2c_egress(store)
    assert len(original) == 1
    assert original[0].payload()["payment_id"] == reference.payment_id
    assert customer.reconcile_invoiced_o2c_egress(
        store, max_new_messages=0,
    ) == original
    assert customer.reconcile_invoiced_o2c_egress(
        store, correlation_id="different-order",
    ) == ()
    with pytest.raises(ValueError, match="max_new_messages"):
        customer.reconcile_invoiced_o2c_egress(store, max_new_messages=-1)
