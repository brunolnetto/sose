"""Replenishment shares the R2R contract but uses a distinct durable intent."""
from datetime import timedelta

import pytest

from sose.composition import trading_company_replenishment as replenishment
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.examples.p2p import simulation as p2p
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


@pytest.mark.parametrize(
    "cut,expected",
    [
        ("ack_before_business", "drafted"),
        ("submitted", "submitted"),
        ("posted_before_command_checkpoint", "posted"),
    ],
)
def test_replenishment_r2r_accepted_effect_resumes_after_worker_death(
    tmp_path, monkeypatch, cut, expected,
):
    path = tmp_path / "replenishment-r2r.sqlite"
    opened = []

    def factory():
        store = SQLiteIncrementalPersistence(path)
        opened.append(store)
        return store

    monkeypatch.setattr(replenishment, "MemoryPersistence", factory)
    original_execute = replenishment._execute_accounting
    original_dispatch = replenishment.r2r._dispatch

    def crash_before(*args, **kwargs):
        if cut == "ack_before_business":
            raise RuntimeError("replenishment accounting worker killed")
        return original_execute(*args, **kwargs)

    def crash_after_dispatch(*args, **kwargs):
        result = original_dispatch(*args, **kwargs)
        if (cut == "submitted" and args[2] == "submit") or (
            cut == "posted_before_command_checkpoint" and args[2] == "post"
        ):
            raise RuntimeError("replenishment accounting worker killed")
        return result

    monkeypatch.setattr(replenishment, "_execute_accounting", crash_before)
    monkeypatch.setattr(replenishment.r2r, "_dispatch", crash_after_dispatch)
    with pytest.raises(RuntimeError, match="replenishment accounting worker killed"):
        replenishment.run_replenishment_path()
    monkeypatch.setattr(replenishment, "_execute_accounting", original_execute)
    monkeypatch.setattr(replenishment.r2r, "_dispatch", original_dispatch)
    opened[-1].close()

    with SQLiteIncrementalPersistence(path) as store:
        journal = next(e for e in store.entities() if e.entity_type == "journal_entry")
        assert journal.state == expected
        worker = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="replenishment-r2r-restart",
            job_id="replenishment-r2r-recovery", max_actions=16,
        )
        result = worker.run_trigger(
            trigger_id="replenishment-r2r-first",
            now=p2p.ORIGIN + timedelta(days=4),
        )
        assert result.actions > 0
        posted = store.entity("journal_entry", journal.id)
        assert posted.state == "posted"
        version = posted.version
        with store.transaction(owner_epoch=store.writer_epoch()) as uow:
            delivery = next(
                d for d in uow.boundary_deliveries()
                if d.contract_name == "accounting.entry_requested"
            )
            receipt = uow.get_boundary_consumption(delivery.delivery_id)
            assert receipt is not None
            assert uow.get_command(receipt.consumer_effect_id) is None
        assert worker.run_trigger(
            trigger_id="replenishment-r2r-second",
            now=p2p.ORIGIN + timedelta(days=4, minutes=1),
        ).actions == 0
        assert store.entity("journal_entry", journal.id).version == version
