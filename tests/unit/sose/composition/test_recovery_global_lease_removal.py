"""#635: recovery under the namespace-wide writer lease was removed in 0.14.0 (deprecated in 0.13.0)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sose.cli import build_parser
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.recovery_trigger import scoped_config_json
from sose.core.resource_identity import ResourcePoolContract
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
