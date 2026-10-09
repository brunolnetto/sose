"""TDD recovery of Logistics effect and delivery egress after worker death."""
from datetime import timedelta

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


@pytest.mark.parametrize("crash_point", ["logistics_after_ack", "delivery_publish"])
def test_logistics_composition_restarts_without_replaying_in_memory_stages(
    tmp_path, monkeypatch, crash_point,
):
    path = tmp_path / "logistics-recovery.sqlite"
    held = []

    def store_factory():
        store = SQLiteIncrementalPersistence(path)
        held.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", store_factory)
    original_publish = customer._publish
    original_execute = customer._execute_intent

    def interrupt_publish(service, *, contract_name, **kwargs):
        if crash_point == "delivery_publish" and contract_name == "logistics.delivery_completed":
            raise RuntimeError("worker killed")
        return original_publish(service, contract_name=contract_name, **kwargs)

    def interrupt_execute(persistence, *, effect_id, fixtures, correlation_id):
        effect = persistence.command(effect_id)
        if (
            crash_point == "logistics_after_ack"
            and effect is not None
            and effect.name == "composition.deliver_shipment"
        ):
            raise RuntimeError("worker killed")
        return original_execute(
            persistence, effect_id=effect_id, fixtures=fixtures, correlation_id=correlation_id,
        )

    monkeypatch.setattr(customer, "_publish", interrupt_publish)
    monkeypatch.setattr(customer, "_execute_intent", interrupt_execute)
    with pytest.raises(RuntimeError, match="worker killed"):
        customer.run_customer_demand_path()
    held[-1].close()
    monkeypatch.setattr(customer, "_publish", original_publish)
    monkeypatch.setattr(customer, "_execute_intent", original_execute)

    with SQLiteIncrementalPersistence(path) as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="recovered-logistics",
            job_id="logistics-recovery-test", max_actions=16,
        )
        first = runner.run_trigger(
            trigger_id="recover-logistics-1",
            now=o2c.ORIGIN + timedelta(days=1),
        )
        assert first.actions >= 1
        with store.transaction(owner_epoch=store.writer_epoch()) as uow:
            deliveries = uow.boundary_deliveries()
            messages = {d.message_id: uow.get_boundary_message(d.message_id) for d in deliveries}
            dispatch = next(m for m in messages.values() if m.contract_key == "warehouse.dispatch_ready.v1")
            delivered = [
                m for m in messages.values()
                if m.contract_key == "logistics.delivery_completed.v1"
            ]
            assert len(delivered) == 1
            assert delivered[0].causation_id == dispatch.message_id
            logistic_delivery = next(d for d in deliveries if d.message_id == dispatch.message_id)
            effect = uow.get_boundary_consumption(logistic_delivery.delivery_id)
            assert effect is not None
            assert uow.get_command(effect.consumer_effect_id) is None
            shipment = uow.get_entity("shipment", dispatch.payload()["shipment_id"])
            assert shipment.state == "delivered"
            initial_identity = (delivered[0].message_id, delivered[0].payload_hash)

        second = runner.run_trigger(
            trigger_id="recover-logistics-2",
            now=o2c.ORIGIN + timedelta(days=1, minutes=1),
        )
        assert second.actions == 0
        with store.transaction(owner_epoch=store.writer_epoch()) as uow:
            delivered = [
                uow.get_boundary_message(d.message_id)
                for d in uow.boundary_deliveries()
                if d.contract_name == "logistics.delivery_completed"
            ]
            assert [(m.message_id, m.payload_hash) for m in delivered] == [initial_identity]


@pytest.mark.parametrize(
    "cut,expected_state",
    [
        ("origin_hub", "at_origin_hub"),
        ("transfer", "at_destination_hub"),
        ("complete", "delivered"),
        ("attempt_only", "out_for_delivery"),
    ],
)
def test_mid_transition_worker_death_resumes_from_durable_logistics_state(
    tmp_path, monkeypatch, cut, expected_state,
):
    path = tmp_path / "partial-logistics.sqlite"
    held = []

    def store_factory():
        store = SQLiteIncrementalPersistence(path)
        held.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", store_factory)
    stage = {
        "origin_hub": "reconcile_origin_hub",
        "transfer": "reconcile_transfer",
        "complete": "reconcile_delivery_success",
    }.get(cut, "_dispatch")
    original = getattr(customer.logistics, stage)

    def crash_after_stage(*args, **kwargs):
        completed = original(*args, **kwargs)
        if cut == "attempt_only":
            entity = args[1]
            if entity.entity_type != "delivery_attempt" or args[2] != "deliver":
                return completed
        raise RuntimeError("crash between durable Logistics phases")

    monkeypatch.setattr(customer.logistics, stage, crash_after_stage)
    with pytest.raises(RuntimeError, match="crash between durable Logistics phases"):
        customer.run_customer_demand_path()
    monkeypatch.setattr(customer.logistics, stage, original)
    held[-1].close()

    with SQLiteIncrementalPersistence(path) as store:
        shipment = next(e for e in store.entities() if e.entity_type == "shipment")
        assert shipment.state == expected_state
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="logistics-stage-restart",
            job_id="recovery-after-midphase", max_actions=16,
        )
        result = runner.run_trigger(
            trigger_id="resume-after-stage", now=o2c.ORIGIN + timedelta(days=1),
        )
        assert result.actions >= 1
        assert store.entity("shipment", shipment.id).state == "delivered"
        assert runner.run_trigger(
            trigger_id="resume-next", now=o2c.ORIGIN + timedelta(days=1, minutes=1),
        ).actions == 0
