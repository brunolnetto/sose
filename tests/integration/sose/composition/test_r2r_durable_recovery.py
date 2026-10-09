"""Crash-falsification tests for final PC6 R2R journal consumer."""
from datetime import timedelta

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


@pytest.mark.parametrize(
    "cut,initial_state",
    [
        ("before_r2r", "drafted"),
        ("after_submit", "submitted"),
        ("after_post", "posted"),
        ("after_effect_before_ack", "posted"),
    ],
)
def test_restarted_r2r_consumer_resumes_committed_journal_without_duplicate(
    tmp_path, monkeypatch, cut, initial_state,
):
    path = tmp_path / "pc6-r2r.sqlite"
    opened = []

    def factory():
        store = SQLiteIncrementalPersistence(path)
        opened.append(store)
        return store

    monkeypatch.setattr(customer, "MemoryPersistence", factory)
    original_execute = customer._execute_intent
    original_dispatch = customer.r2r._dispatch
    original_submit = customer.r2r.submit_and_post_journal

    def interrupted_execute(persistence, *, effect_id, fixtures, correlation_id):
        effect = persistence.command(effect_id)
        if cut == "before_r2r" and effect is not None and (
            effect.name == "composition.post_customer_journal"
        ):
            raise RuntimeError("R2R worker died")
        return original_execute(
            persistence, effect_id=effect_id, fixtures=fixtures, correlation_id=correlation_id,
        )

    def interrupted_dispatch(*args, **kwargs):
        result = original_dispatch(*args, **kwargs)
        if (
            cut == "after_submit" and args[2] == "submit"
        ) or (
            cut == "after_post" and args[2] == "post"
        ):
            raise RuntimeError("R2R worker died")
        return result

    def interrupted_submit(*args, **kwargs):
        result = original_submit(*args, **kwargs)
        if cut == "after_effect_before_ack":
            raise RuntimeError("R2R worker died")
        return result

    monkeypatch.setattr(customer, "_execute_intent", interrupted_execute)
    monkeypatch.setattr(customer.r2r, "_dispatch", interrupted_dispatch)
    monkeypatch.setattr(customer.r2r, "submit_and_post_journal", interrupted_submit)
    with pytest.raises(RuntimeError, match="R2R worker died"):
        customer.run_customer_demand_path()
    monkeypatch.setattr(customer, "_execute_intent", original_execute)
    monkeypatch.setattr(customer.r2r, "_dispatch", original_dispatch)
    monkeypatch.setattr(customer.r2r, "submit_and_post_journal", original_submit)
    opened[-1].close()

    with SQLiteIncrementalPersistence(path) as store:
        journal = next(
            e for e in store.entities() if e.entity_type == "journal_entry"
        )
        assert journal.state == initial_state
        worker = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="r2r-restart",
            job_id="r2r-recovery", max_actions=16,
        )
        result = worker.run_trigger(
            trigger_id="r2r-recover", now=o2c.ORIGIN + timedelta(days=3),
        )
        assert result.actions >= 1
        posted = store.entity("journal_entry", journal.id)
        assert posted.state == "posted"
        with store.transaction(owner_epoch=store.writer_epoch()) as uow:
            deliveries = uow.boundary_deliveries()
            accounting = [
                uow.get_boundary_message(d.message_id)
                for d in deliveries if d.contract_name == "accounting.entry_requested"
            ]
            assert len(accounting) == 1
            item = next(d for d in deliveries if d.message_id == accounting[0].message_id)
            receipt = uow.get_boundary_consumption(item.delivery_id)
            assert receipt is not None and uow.get_command(receipt.consumer_effect_id) is None
        version = posted.version
        assert worker.run_trigger(
            trigger_id="r2r-recover-next", now=o2c.ORIGIN + timedelta(days=3, minutes=1),
        ).actions == 0
        assert store.entity("journal_entry", journal.id).version == version
        assert worker.run_trigger(
            trigger_id="r2r-recover", now=o2c.ORIGIN + timedelta(days=3),
        ).actions == 0
