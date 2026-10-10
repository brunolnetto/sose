"""Each organization has its own durable trigger cursor and failure domain."""
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.scheduler import OrganizationRecoveryFleet, RecoverySchedule
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


START = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def _schedule(store, organization):
    return RecoverySchedule(
        TradingCustomerRecoveryRunner(
            persistence=store, owner_id=f"worker-{organization}",
            job_id=f"pc6-{organization}", max_actions=1,
        ),
        START, timedelta(minutes=1), max_slots=2,
    )


def test_fleet_restarts_failed_organization_without_rewinding_an_independent_job(
    tmp_path, monkeypatch,
):
    a_path, b_path = tmp_path / "a.db", tmp_path / "b.db"
    original = TradingCustomerRecoveryRunner._run_bounded

    def fail_org_a(self, store, *, now):
        if self.job_id == "pc6-a":
            raise RuntimeError("injected death after durable trigger claim")
        return original(self, store, now=now)

    with SQLiteIncrementalPersistence(a_path) as a, SQLiteIncrementalPersistence(b_path) as b:
        fleet = OrganizationRecoveryFleet({"a": _schedule(a, "a"), "b": _schedule(b, "b")})
        monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", fail_org_a)
        result = fleet.run_due(now=START)
        assert set(result.failures) == {"a"}
        assert "RuntimeError" in result.failures["a"]
        assert len(result.completed["b"]) == 1
        assert a.job_state("pc6-a").active_trigger_id is not None
        assert b.job_state("pc6-b").next_tick == 1

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", original)
    with SQLiteIncrementalPersistence(a_path) as a, SQLiteIncrementalPersistence(b_path) as b:
        fleet = OrganizationRecoveryFleet({"a": _schedule(a, "a"), "b": _schedule(b, "b")})
        result = fleet.run_due(now=START + timedelta(minutes=1))
        assert not result.failures
        assert len(result.completed["a"]) == 2  # incomplete 0, then 1
        assert len(result.completed["b"]) == 1  # only slot 1
        assert a.job_state("pc6-a").next_tick == 2
        assert b.job_state("pc6-b").next_tick == 2
        assert fleet.run_due(now=START + timedelta(minutes=1)).completed == {}


def test_fleet_rejects_shared_operational_ownership(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "one.db") as store:
        first = _schedule(store, "a")
        second = _schedule(store, "b")
        with pytest.raises(ValueError, match="distinct"):
            OrganizationRecoveryFleet({"a": first, "b": second})
