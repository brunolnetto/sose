"""Restartable deterministic catch-up scheduling for PC6 boundary recovery."""
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import RecoverySchedule
from sose.jobs.runner import scheduled_trigger_id
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


START = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def make_scheduler(store):
    return RecoverySchedule(
        runner=TradingCustomerRecoveryRunner(
            persistence=store, owner_id="composition-scheduler", max_actions=3,
        ),
        start_at=START,
        interval=timedelta(minutes=1),
        max_slots=2,
    )


def test_scheduler_replays_missed_slots_exactly_once_after_restart(tmp_path):
    db = tmp_path / "scheduler.sqlite"
    with SQLiteIncrementalPersistence(db) as store:
        scheduler = make_scheduler(store)
        result = scheduler.run_due(now=START + timedelta(minutes=4))
        assert [x.trigger_id for x in result] == [
            scheduled_trigger_id(scheduler.runner.job_id, START),
            scheduled_trigger_id(scheduler.runner.job_id, START + timedelta(minutes=1)),
        ]
        assert scheduler.run_due(now=START + timedelta(minutes=1)) == ()

    with SQLiteIncrementalPersistence(db) as recovered:
        scheduler = make_scheduler(recovered)
        result = scheduler.run_due(now=START + timedelta(minutes=4))
        assert len(result) == 2
        assert result[0].trigger_id == scheduled_trigger_id(
            scheduler.runner.job_id, START + timedelta(minutes=2)
        )
        assert result[1].trigger_id == scheduled_trigger_id(
            scheduler.runner.job_id, START + timedelta(minutes=3)
        )
        result = scheduler.run_due(now=START + timedelta(minutes=4))
        assert len(result) == 1
        assert result[0].trigger_id == scheduled_trigger_id(
            scheduler.runner.job_id, START + timedelta(minutes=4)
        )
        assert scheduler.run_due(now=START + timedelta(minutes=4)) == ()
        assert recovered.job_state(scheduler.runner.job_id).run_count == 5


def test_crashed_slot_is_retried_before_later_slots(tmp_path, monkeypatch):
    db = tmp_path / "crashed.sqlite"
    with SQLiteIncrementalPersistence(db) as store:
        scheduler = make_scheduler(store)
        original = TradingCustomerRecoveryRunner._run_bounded
        calls = {"count": 0}

        def interrupt_once(self, persistence, *, now):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("worker killed before completion checkpoint")
            return original(self, persistence, now=now)

        monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", interrupt_once)
        with pytest.raises(RuntimeError, match="worker killed"):
            scheduler.run_due(now=START + timedelta(minutes=2))
        state = store.job_state(scheduler.runner.job_id)
        assert state.active_trigger_id == scheduled_trigger_id(scheduler.runner.job_id, START)

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", original)
    with SQLiteIncrementalPersistence(db) as recovered:
        scheduler = make_scheduler(recovered)
        result = scheduler.run_due(now=START + timedelta(minutes=2))
        assert [x.trigger_id for x in result] == [
            scheduled_trigger_id(scheduler.runner.job_id, START),
            scheduled_trigger_id(scheduler.runner.job_id, START + timedelta(minutes=1)),
        ]
        assert scheduler.run_due(now=START + timedelta(minutes=2))[0].trigger_id == (
            scheduled_trigger_id(scheduler.runner.job_id, START + timedelta(minutes=2))
        )
        assert recovered.job_state(scheduler.runner.job_id).run_count == 3


def test_scheduler_rejects_invalid_time_and_interval(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "invalid.sqlite") as store:
        runner = TradingCustomerRecoveryRunner(persistence=store, owner_id="worker")
        with pytest.raises(ValueError, match="interval"):
            RecoverySchedule(runner, START, timedelta(0))
        with pytest.raises(ValueError, match="timezone"):
            RecoverySchedule(runner, START.replace(tzinfo=None), timedelta(minutes=1))
        scheduler = RecoverySchedule(runner, START, timedelta(minutes=1))
        with pytest.raises(ValueError, match="timezone"):
            scheduler.run_due(now=START.replace(tzinfo=None))
        assert scheduler.run_due(now=START - timedelta(minutes=1)) == ()
