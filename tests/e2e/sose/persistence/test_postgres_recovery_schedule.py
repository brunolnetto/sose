"""Restartable deterministic catch-up scheduling for PC6 boundary recovery.

PostgreSQL policy-mode port of the deprecated SQLite recovery (#634); restart
is a fresh PostgresPersistence connection on the same namespace.
"""
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import RecoverySchedule
from sose.jobs.runner import scheduled_trigger_id
from sose.core.resource_identity import ResourcePoolContract
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")

# Certified, but never issued here (these schedules run on empty ledgers):
# mapping it only selects policy mode without changing admission (#634).
NEUTRAL_POLICY = {
    "composition.post_replenishment_journal": ResourcePoolContract(
        resource_type="journal_clerk", pool_id="recovery-schedule-neutral",
        scope="shared", capacity=1,
    ),
}


START = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
# Explicit fixture binding; no flow exists on these empty ledgers.
FIXTURE_ORGANIZATIONS = {"recovery-schedule-fixture": "test-org-recovery-schedule"}


def _namespace():
    return "recovery_schedule_" + uuid4().hex[:12]


def make_scheduler(store):
    return RecoverySchedule(
        runner=TradingCustomerRecoveryRunner(
            persistence=store, owner_id="composition-scheduler", max_actions=3,
            resource_policies=NEUTRAL_POLICY, resource_organizations=FIXTURE_ORGANIZATIONS,
        ),
        start_at=START,
        interval=timedelta(minutes=1),
        max_slots=2,
    )


def test_scheduler_replays_missed_slots_exactly_once_after_restart():
    namespace = "recovery_schedule_" + uuid4().hex[:12]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        scheduler = make_scheduler(store)
        result = scheduler.run_due(now=START + timedelta(minutes=4))
        assert [x.trigger_id for x in result] == [
            scheduled_trigger_id(scheduler.runner.job_id, START),
            scheduled_trigger_id(scheduler.runner.job_id, START + timedelta(minutes=1)),
        ]
        assert scheduler.run_due(now=START + timedelta(minutes=1)) == ()

    with PostgresPersistence(DSN, namespace=namespace) as recovered:
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


def test_crashed_slot_is_retried_before_later_slots(monkeypatch):
    namespace = "recovery_schedule_" + uuid4().hex[:12]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        scheduler = make_scheduler(store)
        original = TradingCustomerRecoveryRunner._run_bounded
        calls = {"count": 0}

        def interrupt_once(self, persistence, *, now, logical_now=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("worker killed before completion checkpoint")
            return original(self, persistence, now=now, logical_now=logical_now)

        monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", interrupt_once)
        with pytest.raises(RuntimeError, match="worker killed"):
            scheduler.run_due(now=START + timedelta(minutes=2))
        state = store.job_state(scheduler.runner.job_id)
        assert state.active_trigger_id == scheduled_trigger_id(scheduler.runner.job_id, START)

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", original)
    with PostgresPersistence(DSN, namespace=namespace) as recovered:
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



def test_replayed_old_slot_uses_current_observation_time_for_lease_expiry(
    monkeypatch,
):
    """A physical lease cannot remain live just because logical slots are old."""
    observed = START + timedelta(hours=4)
    lease_times = []
    original = TradingCustomerRecoveryRunner._run_bounded

    def record_lease_clock(self, store, *, now, logical_now=None):
        lease_times.append(now)
        return original(self, store, now=now, logical_now=logical_now)

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", record_lease_clock)
    with PostgresPersistence(DSN, namespace=_namespace()) as store:
        schedule = RecoverySchedule(
            make_scheduler(store).runner, START, timedelta(minutes=1), max_slots=3,
        )
        slots = schedule.run_due(now=observed)
        assert len(slots) == 3
        assert lease_times == [observed, observed, observed]
        assert store.job_state(schedule.runner.job_id).logical_time == (
            START + timedelta(minutes=2)
        )


def test_scheduler_rejects_invalid_time_and_interval():
    with PostgresPersistence(DSN, namespace=_namespace()) as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker",
            resource_policies=NEUTRAL_POLICY, resource_organizations=FIXTURE_ORGANIZATIONS,
        )
        with pytest.raises(ValueError, match="interval"):
            RecoverySchedule(runner, START, timedelta(0))
        with pytest.raises(ValueError, match="timezone"):
            RecoverySchedule(runner, START.replace(tzinfo=None), timedelta(minutes=1))
        scheduler = RecoverySchedule(runner, START, timedelta(minutes=1))
        with pytest.raises(ValueError, match="timezone"):
            scheduler.run_due(now=START.replace(tzinfo=None))
        assert scheduler.run_due(now=START - timedelta(minutes=1)) == ()
