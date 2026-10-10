"""A durable transient resource demand is never a terminal business failure."""
from datetime import datetime, timezone

from sose.composition import trading_company_customer as customer
from sose.composition.model import BoundaryMessage
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.events import Command
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def test_recovery_defers_a_transient_physical_wait_to_the_next_recurring_tick(
    tmp_path, monkeypatch,
):
    source = BoundaryMessage.create(
        contract_name="warehouse.dispatch_ready", contract_version=1,
        source_domain="warehouse_fulfillment", source_identity="order",
        destination_domain="logistics", occurrence_key="pickup",
        correlation_id="organization-a", causation_id=None, produced_at=NOW,
        payload={"shipment_id": "shipment-a"},
    )
    cmd = Command(
        command_id="pending-delivery", name="composition.deliver_shipment",
        entity_type="shipment", entity_id="shipment-a", due_at=NOW, issued_at=NOW,
        causation_id=source.message_id, correlation_id=source.correlation_id,
    )
    attempts = []

    def pending(store):
        return ((source, cmd.command_id),) if store.command(cmd.command_id) else ()

    def process(store, source, effect_id):
        attempts.append(effect_id)
        if len(attempts) == 1:
            raise customer.DurableResourceWait("pickup_courier", "pickup-courier:shipment-a")
        with store.transaction() as uow:
            uow.delete_command(effect_id)

    monkeypatch.setattr(
        TradingCustomerRecoveryRunner, "_pending_effects", staticmethod(pending),
    )
    monkeypatch.setattr(
        TradingCustomerRecoveryRunner, "_execute_pending", staticmethod(process),
    )

    database = tmp_path / "durable-pickup.sqlite"
    with SQLiteIncrementalPersistence(database) as store:
        with store.transaction() as uow:
            uow.save_command(cmd)
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker-before-death", max_actions=3,
        )
        first = runner.run_scheduled_trigger(scheduled_for=NOW)
        assert first.actions == 0
        assert store.command(cmd.command_id) == cmd
        assert attempts == [cmd.command_id]
    # A different worker and a new persistence object replay the original
    # durable Command on a different recurring slot.
    with SQLiteIncrementalPersistence(database) as restored:
        runner = TradingCustomerRecoveryRunner(
            persistence=restored, owner_id="worker-after-restart", max_actions=3,
        )
        second = runner.run_scheduled_trigger(scheduled_for=NOW.replace(minute=1))
        assert second.actions == 1
        assert attempts == [cmd.command_id, cmd.command_id]
        assert restored.command(cmd.command_id) is None
        assert runner.run_scheduled_trigger(
            scheduled_for=NOW.replace(minute=2),
        ).actions == 0


def test_synchronous_reference_path_retries_only_typed_temporary_capacity_wait(
    monkeypatch,
):
    original = customer._execute_intent
    attempts = []

    def transient_once(store, *, effect_id, fixtures, correlation_id):
        effect = store.command(effect_id)
        if effect is not None and effect.name == "composition.deliver_shipment":
            attempts.append(effect_id)
            if len(attempts) == 1:
                raise customer.DurableResourceWait(
                    "pickup_courier", f"pickup-courier:{effect.entity_id}",
                )
        return original(
            store, effect_id=effect_id, fixtures=fixtures,
            correlation_id=correlation_id,
        )

    monkeypatch.setattr(customer, "_execute_intent", transient_once)
    result = customer.run_customer_demand_path()
    assert len(attempts) == 2
    assert result.persistence.entity("shipment", result.shipment_id).state == "delivered"
