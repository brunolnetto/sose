"""#635: recovery under the namespace-wide writer lease was removed in 0.14.0 (deprecated in 0.13.0)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sose.cli import build_parser
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.recovery_rebind import RecoveryRebindError, rebind_recovery_job_to_scope
from sose.composition.recovery_trigger import scoped_config_json
from sose.core.resource_identity import ResourcePoolContract
from sose.persistence.ownership import FencedEnginePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from tests.support.global_lease_job import RECOVERY_JOB, seed_global_lease_job

POLICY = {"composition.post_replenishment_journal": ResourcePoolContract(
    resource_type="journal_clerk", pool_id="neutral", scope="shared", capacity=1,
)}
T0 = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)


def _runner(store, **overrides):
    return TradingCustomerRecoveryRunner(**{
        "persistence": store, "owner_id": "worker", "correlation_id": "flow",
        "organization_owners": {"flow": "org"}, "resource_policies": POLICY,
        "resource_organizations": {"flow": "org"}, **overrides,
    })


def test_the_global_lease_is_refused(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        with pytest.raises(TypeError, match="was removed in SOSE 0.14.0"):
            _runner(store, scoped_writer=False)


def test_the_runner_is_scoped_by_default_and_needs_its_correlation(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        assert _runner(store).scoped_writer is True
        with pytest.raises(ValueError, match="one explicit correlation_id"):
            _runner(store, correlation_id=None)


def test_a_0_13_global_job_is_refused_until_rebound(tmp_path, monkeypatch):
    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded",
                        lambda self, store, *, now, logical_now=None: 0)
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        seed_global_lease_job(store, [T0])
        epoch = store.writer_epoch()
        with pytest.raises(ValueError, match="--rebind-scoped"):
            _runner(store).run_scheduled_trigger(scheduled_for=T0)
        assert store.job_state(RECOVERY_JOB).config_json == "{}"
        assert store.writer_epoch() == epoch            # refused before any claim
        assert store.scoped_writer_epoch(RECOVERY_JOB) == 0


def test_a_0_13_job_interrupted_mid_occurrence_points_back_to_0_13_not_in_a_circle(
    tmp_path, monkeypatch,
):
    """Review of #673 (B1): neither refusal may send the operator to the other one."""
    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded",
                        lambda self, store, *, now, logical_now=None: 0)
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        seed_global_lease_job(store, [T0])
        # A 0.13 global worker died after activating its next occurrence.
        lease = store.claim_writer("dead-0.13-worker", expected_epoch=store.writer_epoch())
        with FencedEnginePersistence(store, lease).transaction() as uow:
            uow.save_job_state(replace(
                uow.get_job_state(RECOVERY_JOB), status="running", active_trigger_id="slot-2",
            ))
        with pytest.raises(ValueError, match="must first be finished with SOSE 0.13"):
            _runner(store).run_scheduled_trigger(scheduled_for=T0)
        with pytest.raises(RecoveryRebindError, match="finish it with SOSE 0.13"):
            rebind_recovery_job_to_scope(
                store, job_id=RECOVERY_JOB, owner_id="operator", correlation_id="flow",
                organization_id="org", now=T0,
            )
        assert store.job_state(RECOVERY_JOB).config_json == "{}"


def test_a_new_job_stores_its_scoped_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded",
                        lambda self, store, *, now, logical_now=None: 0)
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        _runner(store).run_scheduled_trigger(scheduled_for=T0)
        assert store.job_state(RECOVERY_JOB).config_json == scoped_config_json("flow", "org")
        assert store.writer_epoch() == 0                # the namespace-wide lease is untouched
        assert store.scoped_writer_epoch(RECOVERY_JOB) == 1


def test_cli_still_accepts_scoped_writer_for_compatibility():
    args = build_parser().parse_args([
        "composition-recovery", "--postgres-dsn", "postgresql://unused/db",
        "--owner-id", "cron", "--correlation-id", "flow", "--organization-id", "org",
        "--scoped-writer",
    ])
    assert args.scoped_writer is True
