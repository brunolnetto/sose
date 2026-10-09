"""PC6 v1 falsification: O2C consumer recovers after Logistics committed delivery."""
from dataclasses import replace
from datetime import timedelta

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


@pytest.mark.parametrize(
    "crash_point",
    ["before_o2c_effect", "before_receivable", "after_receivable"],
)
def test_restart_recovers_o2c_and_payment_request_from_durable_delivery(
    tmp_path, monkeypatch, crash_point,
):
    path = tmp_path / "o2c-restart.sqlite"
    opened = []

    def factory():
        store = SQLiteIncrementalPersistence(path)
        opened.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", factory)
    original_execute = customer._execute_intent
    original_receivable = customer.o2c.ensure_receivable

    def interrupted_execute(persistence, *, effect_id, fixtures, correlation_id):
        effect = persistence.command(effect_id)
        if crash_point == "before_o2c_effect" and effect is not None and (
            effect.name == "composition.complete_external_fulfillment"
        ):
            raise RuntimeError("worker killed at O2C")
        return original_execute(
            persistence, effect_id=effect_id, fixtures=fixtures,
            correlation_id=correlation_id,
        )

    def interrupted_receivable(*args, **kwargs):
        if crash_point == "before_receivable":
            raise RuntimeError("worker killed at O2C")
        result = original_receivable(*args, **kwargs)
        if crash_point == "after_receivable":
            raise RuntimeError("worker killed at O2C")
        return result

    monkeypatch.setattr(customer, "_execute_intent", interrupted_execute)
    monkeypatch.setattr(customer.o2c, "ensure_receivable", interrupted_receivable)
    with pytest.raises(RuntimeError, match="worker killed at O2C"):
        customer.run_customer_demand_path()

    monkeypatch.setattr(customer, "_execute_intent", original_execute)
    monkeypatch.setattr(customer.o2c, "ensure_receivable", original_receivable)
    opened[-1].close()
    with SQLiteIncrementalPersistence(path) as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="o2c-restart",
            job_id="o2c-recovery-test", max_actions=16,
        )
        first = runner.run_trigger(
            trigger_id="o2c-recovery-1", now=o2c.ORIGIN + timedelta(days=1),
        )
        assert first.actions > 0
        with store.transaction(owner_epoch=store.writer_epoch()) as uow:
            deliveries = uow.boundary_deliveries()
            msgs = {
                d.message_id: uow.get_boundary_message(d.message_id)
                for d in deliveries
            }
            inbound = next(
                m for m in msgs.values()
                if m.contract_key == "logistics.delivery_completed.v1"
            )
            payment_requests = [
                m for m in msgs.values()
                if m.contract_key == "o2c.payment_requested.v1"
            ]
            assert len(payment_requests) == 1
            outbound = payment_requests[0]
            assert outbound.causation_id == inbound.message_id
            order_id = inbound.payload()["order_id"]
            order = uow.get_entity("sales_order", order_id)
            assert order is not None and order.state == "invoiced"
            receivable = uow.get_entity("receivable", o2c.receivable_id(order_id))
            assert receivable is not None and receivable.state == "open"
            inbound_delivery = next(d for d in deliveries if d.message_id == inbound.message_id)
            receipt = uow.get_boundary_consumption(inbound_delivery.delivery_id)
            assert receipt is not None
            assert uow.get_command(receipt.consumer_effect_id) is None
            assert outbound.payload()["order_id"] == order_id
            assert outbound.payload()["payment_id"]
            assert outbound.payload()["amount"] == order.attributes["amount"]
        second = runner.run_trigger(
            trigger_id="o2c-recovery-2",
            now=o2c.ORIGIN + timedelta(days=1, minutes=1),
        )
        assert second.actions == 0
        with store.transaction(owner_epoch=store.writer_epoch()) as uow:
            persisted = uow.get_boundary_message(outbound.message_id)
            assert persisted == outbound
        assert runner.run_trigger(
            trigger_id="o2c-recovery-1", now=o2c.ORIGIN + timedelta(days=1),
        ).actions == 0


def test_payment_egress_requires_ack_and_completed_o2c_effect(tmp_path, monkeypatch):
    db = tmp_path / "payment-evidence.sqlite"
    opened = []

    def factory():
        store = SQLiteIncrementalPersistence(db)
        opened.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", factory)
    original_execute = customer._execute_intent

    def interrupted(persistence, *, effect_id, fixtures, correlation_id):
        effect = persistence.command(effect_id)
        if effect is not None and effect.name == "composition.complete_external_fulfillment":
            raise RuntimeError("crash before O2C effect")
        return original_execute(
            persistence, effect_id=effect_id, fixtures=fixtures,
            correlation_id=correlation_id,
        )

    monkeypatch.setattr(customer, "_execute_intent", interrupted)
    with pytest.raises(RuntimeError, match="crash before O2C"):
        customer.run_customer_demand_path()
    monkeypatch.setattr(customer, "_execute_intent", original_execute)
    opened[-1].close()

    with SQLiteIncrementalPersistence(db) as store:
        assert customer.reconcile_invoiced_o2c_egress(store) == ()
        with store.transaction() as uow:
            inbound = next(
                uow.get_boundary_message(d.message_id)
                for d in uow.boundary_deliveries()
                if d.contract_name == "logistics.delivery_completed"
            )
        assert inbound is not None
        worker = TradingCustomerRecoveryRunner(
            store, owner_id="o2c", job_id="o2c-proof", max_actions=16,
        )
        worker.run_trigger(
            trigger_id="apply-o2c", now=o2c.ORIGIN + timedelta(days=1),
        )
        assert len(customer.reconcile_invoiced_o2c_egress(store)) == 1
        assert customer.reconcile_invoiced_o2c_egress(store, max_new_messages=0)
        assert not customer.reconcile_invoiced_o2c_egress(
            store, correlation_id="unrelated-order",
        )
        with pytest.raises(ValueError, match="max_new_messages"):
            customer.reconcile_invoiced_o2c_egress(store, max_new_messages=-1)


def test_payment_egress_rejects_tampered_durable_existing_output(tmp_path, monkeypatch):
    path = tmp_path / "tampered.sqlite"
    opened = []

    def factory():
        store = SQLiteIncrementalPersistence(path)
        opened.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", factory)
    customer.run_customer_demand_path()
    opened[-1].close()

    with SQLiteIncrementalPersistence(path) as store:
        with store.transaction() as uow:
            payment = next(
                uow.get_boundary_message(d.message_id)
                for d in uow.boundary_deliveries()
                if d.contract_name == "o2c.payment_requested"
            )
            assert payment is not None
        assert customer.reconcile_invoiced_o2c_egress(store) == (payment,)
        from sose.composition.model import BoundaryMessage
        corrupt = BoundaryMessage.create(
            contract_name=payment.contract_name, contract_version=payment.contract_version,
            source_domain=payment.source_domain, source_identity=payment.source_identity,
            destination_domain=payment.destination_domain,
            occurrence_key=payment.occurrence_key, correlation_id=payment.correlation_id,
            causation_id=payment.causation_id, produced_at=payment.produced_at,
            payload={**payment.payload(), "payment_id": "wrong"},
        )
        with store.transaction() as uow:
            uow.save_boundary_message(corrupt)
