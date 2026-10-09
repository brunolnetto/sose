"""Executable restart-trigger contract for the Trading Company WM boundary.

The one-shot customer demo is only a fixture; after death the *new worker* must
read the authoritative SQLite-incremental records, with no remembered stage list.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.ownership import FencedEnginePersistence
from sose.persistence.sqlite_incremental import (
    SQLiteIncrementalPersistence,
    StaleWriterError,
)


@pytest.mark.parametrize("crash_point", ["pick_publish", "ack_before_business_effect", "dispatch_publish"])
def test_recurring_recovery_trigger_resumes_durable_customer_boundary(
    tmp_path, monkeypatch, crash_point,
):
    path = tmp_path / "trading-recovery.sqlite3"
    held = []
    def store_factory():
        store = SQLiteIncrementalPersistence(path)
        held.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", store_factory)
    original_publish = customer._publish
    original_execute = customer._execute_intent
    def crashing_publish(service, *, contract_name, **kwargs):
        if (
            (crash_point == "pick_publish" and
             contract_name == "warehouse.inventory_consumption_requested")
            or (crash_point == "dispatch_publish" and
                contract_name == "warehouse.dispatch_ready")
        ):
            raise RuntimeError("simulated process death")
        return original_publish(service, contract_name=contract_name, **kwargs)

    def crashing_execute(persistence, *, effect_id, fixtures, correlation_id):
        intent = persistence.command(effect_id)
        if (
            crash_point == "ack_before_business_effect"
            and intent is not None
            and intent.name == "composition.consume_fulfillment_inventory"
        ):
            raise RuntimeError("simulated process death")
        return original_execute(
            persistence, effect_id=effect_id, fixtures=fixtures,
            correlation_id=correlation_id,
        )

    monkeypatch.setattr(customer, "_publish", crashing_publish)
    monkeypatch.setattr(customer, "_execute_intent", crashing_execute)
    with pytest.raises(RuntimeError, match="simulated process death"):
        customer.run_customer_demand_path()
    held[-1].close()
    monkeypatch.setattr(customer, "_publish", original_publish)
    monkeypatch.setattr(customer, "_execute_intent", original_execute)

    recovered = SQLiteIncrementalPersistence(path)
    now = o2c.ORIGIN + timedelta(days=1)
    worker = TradingCustomerRecoveryRunner(
        persistence=recovered, owner_id="new-worker",
        job_id="trading-pc6-recovery-test",
    )
    first = worker.run_trigger(trigger_id="scheduled-0001", now=now)
    assert first.actions >= 1
    assert recovered.job_state("trading-pc6-recovery-test").next_tick == 1

    with recovered.transaction(owner_epoch=recovered.writer_epoch()) as uow:
        messages = [
            uow.get_boundary_message(d.message_id)
            for d in uow.boundary_deliveries()
        ]
        consumption = next(
            m for m in messages
            if m is not None
            and m.contract_key == "warehouse.inventory_consumption_requested.v1"
        )
        dispatch = next(
            m for m in messages
            if m is not None and m.contract_key == "warehouse.dispatch_ready.v1"
        )
        assert dispatch.causation_id == consumption.message_id
        consumption_deliveries = [
            d for d in uow.boundary_deliveries()
            if d.message_id == consumption.message_id
        ]
        assert len(consumption_deliveries) == 1
        assert uow.get_boundary_consumption(consumption_deliveries[0].delivery_id)
    order = recovered.entity(
        "warehouse_fulfillment_order",
        consumption.payload()["fulfillment_order_id"],
    )
    assert order.state == "shipped"
    stock = recovered.entity(
        "warehouse_management_stock", consumption.payload()["stock_id"],
    )
    assert stock.attributes["external_reservations"][
        consumption.payload()["reservation_reference"]
    ]["consumed"] is True

    original = tuple((e.entity_type, e.id, e.state, e.version) for e in recovered.entities())
    again = worker.run_trigger(trigger_id="scheduled-0001", now=now)
    assert again.actions == 0
    no_work = worker.run_trigger(trigger_id="scheduled-0002", now=now + timedelta(minutes=1))
    assert no_work.actions == 0
    assert original == tuple((e.entity_type, e.id, e.state, e.version) for e in recovered.entities())
    assert recovered.job_state("trading-pc6-recovery-test").next_tick == 2
    recovered.close()


def test_stale_composition_writer_cannot_commit_after_recovery_trigger(tmp_path):
    path = tmp_path / "fenced-recovery.sqlite3"
    persistence = SQLiteIncrementalPersistence(path)
    old = persistence.claim_writer("worker-old", expected_epoch=persistence.writer_epoch())
    worker = TradingCustomerRecoveryRunner(
        persistence=persistence, owner_id="worker-new", job_id="empty-composition",
    )
    first = worker.run_scheduled_trigger(scheduled_for=o2c.ORIGIN)
    assert first.actions == 0
    assert first.trigger_id.startswith("empty-composition:scheduled:")
    assert worker.run_scheduled_trigger(scheduled_for=o2c.ORIGIN).actions == 0
    assert worker.run_scheduled_trigger(
        scheduled_for=o2c.ORIGIN + timedelta(minutes=1)
    ).actions == 0
    # A delayed retry of the FIRST occurrence remains a true no-op even after
    # another scheduled occurrence advanced the durable checkpoint.
    assert worker.run_scheduled_trigger(scheduled_for=o2c.ORIGIN).actions == 0
    assert persistence.job_state("empty-composition").next_tick == 2
    with pytest.raises(StaleWriterError):
        with FencedEnginePersistence(persistence, old).transaction() as uow:
            uow.save_job_state(persistence.job_state("empty-composition"))
    persistence.close()
