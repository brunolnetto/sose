"""Payments restart falsification at each durable statechart boundary."""
from datetime import timedelta

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


@pytest.mark.parametrize(
    "cut,expected_payment_state",
    [
        ("ack_before_effect", "authorization_requested"),
        ("after_authorization", "authorized"),
        ("after_capture_before_schedule", "captured"),
        ("after_capture_and_schedule", "captured"),
        ("after_settlement", "settled"),
    ],
)
def test_payment_continuation_restarts_from_persisted_stage_and_emits_r2r(
    tmp_path, monkeypatch, cut, expected_payment_state,
):
    path = tmp_path / "payment-recovery.sqlite"
    held = []

    def factory():
        store = SQLiteIncrementalPersistence(path)
        held.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", factory)
    originals = {
        "execute": customer._execute_intent,
        "authorize": customer.payments.reconcile_authorization,
        "capture": customer.payments.reconcile_capture_and_schedule_settlement,
        "settle": customer.payments.reconcile_settlement,
        "dispatch": customer.payments._dispatch,
    }

    def crash_before_effect(persistence, *, effect_id, fixtures, correlation_id):
        current = persistence.command(effect_id)
        if cut == "ack_before_effect" and current is not None and (
            current.name == "composition.settle_customer_payment"
        ):
            raise RuntimeError("worker death in Payments")
        return originals["execute"](
            persistence, effect_id=effect_id, fixtures=fixtures, correlation_id=correlation_id,
        )

    def crash_after(target, *args, **kwargs):
        result = originals[target](*args, **kwargs)
        if cut == {
            "authorize": "after_authorization",
            "capture": "after_capture_and_schedule",
            "settle": "after_settlement",
        }[target]:
            raise RuntimeError("worker death in Payments")
        return result

    def crash_dispatch(*args, **kwargs):
        result = originals["dispatch"](*args, **kwargs)
        if cut == "after_capture_before_schedule" and args[2] == "capture":
            raise RuntimeError("worker death in Payments")
        return result

    monkeypatch.setattr(customer, "_execute_intent", crash_before_effect)
    monkeypatch.setattr(customer.payments, "reconcile_authorization",
                        lambda *a, **kw: crash_after("authorize", *a, **kw))
    monkeypatch.setattr(customer.payments, "reconcile_capture_and_schedule_settlement",
                        lambda *a, **kw: crash_after("capture", *a, **kw))
    monkeypatch.setattr(customer.payments, "reconcile_settlement",
                        lambda *a, **kw: crash_after("settle", *a, **kw))
    monkeypatch.setattr(customer.payments, "_dispatch", crash_dispatch)
    with pytest.raises(RuntimeError, match="worker death in Payments"):
        customer.run_customer_demand_path()

    monkeypatch.setattr(customer, "_execute_intent", originals["execute"])
    monkeypatch.setattr(customer.payments, "reconcile_authorization", originals["authorize"])
    monkeypatch.setattr(
        customer.payments, "reconcile_capture_and_schedule_settlement", originals["capture"]
    )
    monkeypatch.setattr(customer.payments, "reconcile_settlement", originals["settle"])
    monkeypatch.setattr(customer.payments, "_dispatch", originals["dispatch"])
    held[-1].close()

    with SQLiteIncrementalPersistence(path) as store:
        payment = next(x for x in store.entities() if x.entity_type == "card_payment")
        assert payment.state == expected_payment_state
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="payment-restart",
            job_id="payment-recovery-test", max_actions=16,
        )
        result = runner.run_trigger(
            trigger_id="payment-first", now=o2c.ORIGIN + timedelta(days=2),
        )
        assert result.actions > 0
        assert store.entity("card_payment", payment.id).state == "settled"
        with store.transaction(owner_epoch=store.writer_epoch()) as uow:
            records = [
                uow.get_boundary_message(d.message_id)
                for d in uow.boundary_deliveries()
            ]
            inbound = next(
                m for m in records if m.contract_key == "o2c.payment_requested.v1"
            )
            outgoing = [
                m for m in records if m.contract_key == "accounting.entry_requested.v1"
            ]
            assert len(outgoing) == 1
            assert outgoing[0].causation_id == inbound.message_id
            assert outgoing[0].payload()["payment_id"] == payment.id
            assert outgoing[0].payload()["journal_id"]
            accepted = next(
                d for d in uow.boundary_deliveries()
                if d.message_id == inbound.message_id
            )
            receipt = uow.get_boundary_consumption(accepted.delivery_id)
            assert receipt is not None and uow.get_command(receipt.consumer_effect_id) is None
        assert runner.run_trigger(
            trigger_id="payment-second", now=o2c.ORIGIN + timedelta(days=2, minutes=1),
        ).actions == 0
        assert runner.run_trigger(
            trigger_id="payment-first", now=o2c.ORIGIN + timedelta(days=2),
        ).actions == 0
