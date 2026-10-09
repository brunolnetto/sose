"""Adversarial validation of domain-scoped, durably fenced PC6 recovery."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.model import (
    BoundaryConsumption, BoundaryDelivery, BoundaryMessage, DeliveryStatus,
)
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.events import Command
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def message(name="accounting.entry_requested", source="cards_payments",
            **payload):
    return BoundaryMessage.create(
        contract_name=name, contract_version=1,
        source_domain=source, source_identity="fixture",
        destination_domain={
            "accounting.entry_requested": "record_to_report",
            "o2c.payment_requested": "cards_payments",
            "logistics.delivery_completed": "order_to_cash",
            "warehouse.dispatch_ready": "logistics",
            "warehouse.inventory_consumption_requested": "warehouse_management",
        }.get(name, "unsupported"),
        occurrence_key="test", correlation_id="corr-1", causation_id=None,
        produced_at=NOW, payload=payload,
    )


@pytest.mark.parametrize(
    "msg,domain,contract",
    [
        (message("warehouse.inventory_consumption_requested", stock_id="stock"),
         "warehouse_management", "warehouse.inventory_consumption_requested"),
        (message("warehouse.dispatch_ready", shipment_id="shipment"),
         "logistics", "warehouse.dispatch_ready"),
        (message("logistics.delivery_completed", order_id="order"),
         "order_to_cash", "logistics.delivery_completed"),
        (message("o2c.payment_requested", payment_id="payment"),
         "cards_payments", "o2c.payment_requested"),
        (message(journal_id="journal"),
         "record_to_report", "accounting.entry_requested"),
        (message(source="procure_to_pay", journal_id="journal"),
         "record_to_report", "accounting.entry_requested"),
    ],
)
def test_registry_only_claims_expected_domain_contract(msg, domain, contract):
    registry = TradingCustomerRecoveryRunner._registry_for(msg)
    assert registry.contracts_for(domain) == frozenset({(contract, 1)})
    assert callable(registry.resolve(msg))
    assert registry.contracts_for("unrelated_domain") == frozenset()


@pytest.mark.parametrize("message_value,reason", [
    (message("other.test"), "unsupported boundary contract"),
    (message(source="unrecognized_producer", journal_id="journal"),
     "unsupported accounting source"),
])
def test_recovery_fails_closed_for_unknown_registry_routes(message_value, reason):
    with pytest.raises(ValueError, match=reason):
        TradingCustomerRecoveryRunner._registry_for(message_value)


@pytest.mark.parametrize("options,reason", [
    ({"owner_id": ""}, "owner and job identity"),
    ({"job_id": ""}, "owner and job identity"),
    ({"max_actions": 0}, "max_actions"),
    ({"lease_duration": timedelta(0)}, "lease_duration"),
    ({"lease_duration": timedelta(seconds=-1)}, "lease_duration"),
])
def test_runner_rejects_invalid_configuration(tmp_path, options, reason):
    with SQLiteIncrementalPersistence(tmp_path / "invalid.sqlite") as persistence:
        with pytest.raises(ValueError, match=reason):
            TradingCustomerRecoveryRunner(
                persistence=persistence, owner_id="worker", **options
            )


def test_runner_refuses_unfenced_authority():
    with pytest.raises(TypeError, match="authoritative writer fencing"):
        TradingCustomerRecoveryRunner(
            persistence=MemoryPersistence(), owner_id="worker",
        )


def accepted(store: MemoryPersistence, contract_name="accounting.entry_requested",
             *, with_consumption=True, command_name="composition.post_customer_journal"):
    upstream = message(contract_name, journal_id="journal")
    delivery = BoundaryDelivery.pending(upstream)
    effect_id = "effect-" + upstream.message_id
    finalized = replace(
        delivery, status=DeliveryStatus.CONSUMED, consumed_at=NOW,
        consumer_effect_id=effect_id,
    )
    with store.transaction() as uow:
        uow.save_boundary_message(upstream)
        uow.save_boundary_delivery(finalized)
        if with_consumption:
            uow.save_boundary_consumption(BoundaryConsumption.create(
                delivery=finalized, consumer_effect_id=effect_id, consumed_at=NOW,
            ))
        uow.save_command(Command(
            command_id=effect_id,
            name=command_name, entity_type="journal_entry",
            entity_id="journal", due_at=NOW,
        ))
    return upstream, finalized, effect_id


def test_pending_effects_detects_missing_consumption_receipt():
    store = MemoryPersistence()
    accepted(store, with_consumption=False)
    with pytest.raises(RuntimeError, match="lacks durable consumption"):
        TradingCustomerRecoveryRunner._pending_effects(store)


def test_pending_effects_detects_missing_message_with_accepted_command():
    store = MemoryPersistence()
    upstream, _, _ = accepted(store)
    store._state.boundary_messages.pop(upstream.message_id)
    with pytest.raises(RuntimeError, match="lacks source message"):
        TradingCustomerRecoveryRunner._pending_effects(store)


@pytest.mark.parametrize("name", [
    "composition.post_customer_journal",
    "composition.post_replenishment_journal",
])
def test_pending_effects_replays_both_journal_intents_and_then_completes(name):
    store = MemoryPersistence()
    upstream, _, effect_id = accepted(store, command_name=name)
    assert TradingCustomerRecoveryRunner._pending_effects(store) == (
        (upstream, effect_id),
    )
    with store.transaction() as uow:
        uow.delete_command(effect_id)
    assert TradingCustomerRecoveryRunner._pending_effects(store) == ()


def test_pending_effects_skips_already_acknowledged_unowned_contract():
    store = MemoryPersistence()
    accepted(store, command_name="composition.some_other_domain_effect")
    assert TradingCustomerRecoveryRunner._pending_effects(store) == ()


def test_run_trigger_rejects_empty_identity_and_time_rewind(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "trigger.sqlite") as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker", job_id="fault-contract-test",
        )
        with pytest.raises(ValueError, match="trigger_id"):
            runner.run_trigger(trigger_id="", now=NOW)
        assert runner.run_trigger(trigger_id="first", now=NOW).actions == 0
        with pytest.raises(ValueError, match="cannot rewind"):
            runner.run_trigger(trigger_id="earlier", now=NOW - timedelta(seconds=1))
        assert runner.run_trigger(
            trigger_id="first", now=NOW - timedelta(seconds=1),
        ).actions == 0
