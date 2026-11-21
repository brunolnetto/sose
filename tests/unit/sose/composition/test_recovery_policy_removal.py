"""#634: policy-less trading-company recovery was removed in 0.11.0 (deprecated in 0.9.0)."""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from sose.cli import build_parser, run_cli
from sose.cli_validation import _recovery_resource_policies
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.resource_identity import ResourcePoolContract
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

REQUIRED = [
    "--owner-id", "cron", "--correlation-id", "flow", "--organization-id", "org",
    "--start-at", "2026-10-09T12:00:00+00:00", "--interval-seconds", "60",
]


def test_runner_without_resource_policies_is_refused(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        with pytest.raises(TypeError, match="policy-less recovery was removed in SOSE 0.11.0"):
            TradingCustomerRecoveryRunner(
                persistence=store, owner_id="worker", correlation_id="flow",
                organization_owners={"flow": "org"},
            )


def test_runner_with_policies_still_constructs_without_postgres(tmp_path):
    """Construction validates only; the PostgreSQL coordinator is built per pass."""
    policy = {"composition.post_replenishment_journal": ResourcePoolContract(
        resource_type="journal_clerk", pool_id="neutral", scope="shared", capacity=1,
    )}
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker", correlation_id="flow",
            organization_owners={"flow": "org"}, resource_policies=policy, resource_organizations={"flow": "org"},
        )
    assert runner.resource_policies == policy


def test_cli_requires_postgres(capsys):
    with pytest.raises(SystemExit) as exited:
        build_parser().parse_args(
            ["composition-recovery", *REQUIRED, "--resource-policies", "p.json"],
        )
    assert exited.value.code == 2
    assert "--postgres-dsn" in capsys.readouterr().err


def test_cli_run_requires_a_policy_file_before_connecting(capsys):
    """#637: optional at parse time (``--rebind-scoped`` needs none), required to run."""
    argv = [
        "composition-recovery", *REQUIRED,
        "--postgres-dsn", "postgresql://unreachable.invalid/db",
    ]
    assert run_cli(argv) == 2
    assert "--resource-policies" in capsys.readouterr().err


def test_cli_sqlite_option_was_removed(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "composition-recovery", "--sqlite", "s.sqlite", "--postgres-dsn", "postgresql://x",
            "--resource-policies", "p.json", *REQUIRED,
        ])
    assert "--sqlite" in capsys.readouterr().err


def test_cli_policy_file_loads_pool_contracts(tmp_path):
    path = tmp_path / "policies.json"
    path.write_text(json.dumps({
        "composition.deliver_shipment": {
            "resource_type": "pickup_courier", "pool_id": "couriers",
            "scope": "shared", "capacity": 2,
        },
    }), encoding="utf-8")
    args = Namespace(resource_policies=str(path), postgres_dsn="postgresql://x")
    assert _recovery_resource_policies(args) == {
        "composition.deliver_shipment": ResourcePoolContract(
            resource_type="pickup_courier", pool_id="couriers", scope="shared", capacity=2,
        ),
    }


@pytest.mark.parametrize("content", ["{}", "[]"])
def test_cli_policy_file_must_be_a_non_empty_object(tmp_path, content):
    path = tmp_path / "policies.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty JSON object"):
        _recovery_resource_policies(Namespace(resource_policies=str(path), postgres_dsn="x"))
