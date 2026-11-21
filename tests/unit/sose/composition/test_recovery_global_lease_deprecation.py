"""#635: recovery under the namespace-wide writer lease is deprecated (0.13.0 -> 0.14.0)."""
from __future__ import annotations

import warnings

import pytest

from sose.cli import run_cli
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.resource_identity import ResourcePoolContract
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

POLICY = {"composition.post_replenishment_journal": ResourcePoolContract(
    resource_type="journal_clerk", pool_id="neutral", scope="shared", capacity=1,
)}
MESSAGE = "TradingCustomerRecoveryRunner with the namespace-wide writer lease"


def _runner(store, **overrides):
    return TradingCustomerRecoveryRunner(
        persistence=store, owner_id="worker", organization_owners={"flow": "org"},
        resource_policies=POLICY, resource_organizations={"flow": "org"}, **overrides,
    )


@pytest.mark.filterwarnings("default::DeprecationWarning")
@pytest.mark.parametrize("overrides", [{}, {"scoped_writer": False}])
def test_global_lease_warns_at_the_call_site(tmp_path, overrides):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        with pytest.warns(DeprecationWarning, match=MESSAGE) as caught:
            _runner(store, **overrides)
    [warning] = [w for w in caught if MESSAGE in str(w.message)]
    text = str(warning.message)
    assert "since SOSE 0.13.0" in text and "removed in SOSE 0.14.0" in text
    assert "scoped_writer=True" in text and "--rebind-scoped" in text
    assert warning.filename == __file__   # ADR-0011 point 3: attributed to the caller


def test_scoped_lease_does_not_warn(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            runner = _runner(store, correlation_id="flow", scoped_writer=True)
    assert runner.scoped_writer is True


@pytest.mark.filterwarnings("default::DeprecationWarning")
def test_invalid_global_runner_raises_before_warning(tmp_path):
    """Validation stays first: a refused configuration is not also told it is deprecated."""
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(ValueError, match="max_actions"):
                _runner(store, max_actions=0)
    assert not [w for w in caught if MESSAGE in str(w.message)]


ARGV = [
    "composition-recovery", "--owner-id", "cron", "--correlation-id", "flow",
    "--organization-id", "org", "--start-at", "2026-10-09T12:00:00+00:00",
    "--interval-seconds", "60", "--resource-policies", "missing-policies.json",
    "--postgres-dsn", "postgresql://unreachable.invalid/db",
]


def test_cli_without_scoped_writer_prints_the_deprecation(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   # the policy file is missing: the run stops right after
    assert run_cli(ARGV) == 2
    err = capsys.readouterr().err
    assert "warning: sose composition-recovery without --scoped-writer" in err
    assert "removed in SOSE 0.14.0" in err and "--rebind-scoped" in err


def test_cli_with_scoped_writer_is_silent(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run_cli([*ARGV, "--scoped-writer"]) == 2
    assert "deprecated" not in capsys.readouterr().err
