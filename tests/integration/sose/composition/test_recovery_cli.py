"""Public CLI drives the same durable catch-up protocol across process restarts."""
import json
import os
import warnings
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.cli import run_cli
from sose.jobs.runner import scheduled_trigger_id
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


START = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def test_composition_recovery_one_shot_cron_invocations(tmp_path, capsys):
    db = tmp_path / "composition.sqlite"
    args = [
        "composition-recovery", "--sqlite", str(db),
        "--owner-id", "cron",
        "--correlation-id", "test-customer-correlation",
        "--organization-id", "test-trading-organization",
        "--start-at", START.isoformat(),
        "--interval-seconds", "60", "--max-slots", "2",
    ]
    assert run_cli([*args, "--now", (START + timedelta(minutes=1)).isoformat()]) == 0
    assert run_cli([*args, "--now", (START + timedelta(minutes=1)).isoformat()]) == 0
    with SQLiteIncrementalPersistence(db) as store:
        job = store.job_state("trading-company-customer-recovery")
        assert job.run_count == 2
        assert job.last_completed_trigger_id == scheduled_trigger_id(
            job.job_id, START + timedelta(minutes=1)
        )
    assert "trading-company-customer-recovery" in capsys.readouterr().out


def test_composition_recovery_cli_rejects_naive_anchor(tmp_path, capsys):
    result = run_cli([
        "composition-recovery", "--sqlite", str(tmp_path / "invalid.sqlite"),
        "--owner-id", "cron",
        "--correlation-id", "test-customer-correlation",
        "--organization-id", "test-trading-organization",
        "--start-at", "2026-10-09T12:00:00",
        "--interval-seconds", "60",
    ])
    assert result == 2
    assert "timezone" in capsys.readouterr().err


@pytest.mark.skipif(
    not os.environ.get("SOSE_TEST_POSTGRES_DSN"), reason="SOSE_TEST_POSTGRES_DSN required",
)
def test_composition_recovery_cli_runs_policy_backed_on_postgres(tmp_path, capsys):
    """#634: the non-deprecated path -- PostgreSQL store plus a resource-policy file."""
    from sose.persistence.postgres import PostgresPersistence

    dsn = os.environ["SOSE_TEST_POSTGRES_DSN"]
    namespace = f"cli_{uuid4().hex[:12]}"
    policies = tmp_path / "policies.json"
    policies.write_text(json.dumps({
        "composition.deliver_shipment": {
            "resource_type": "pickup_courier", "pool_id": "cli-couriers",
            "scope": "shared", "capacity": 1,
        },
    }), encoding="utf-8")
    args = [
        "composition-recovery", "--postgres-dsn", dsn, "--namespace", namespace,
        "--resource-policies", str(policies),
        "--owner-id", "cron",
        "--correlation-id", "test-customer-correlation",
        "--organization-id", "test-trading-organization",
        "--start-at", START.isoformat(),
        "--interval-seconds", "60", "--max-slots", "2",
        "--now", (START + timedelta(minutes=1)).isoformat(),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)   # policy-backed path never warns
        assert run_cli(args) == 0
        assert run_cli(args) == 0   # same due slots again: idempotent catch-up
    with PostgresPersistence(dsn, namespace=namespace) as store:
        job = store.job_state("trading-company-customer-recovery")
        assert job.run_count == 2   # anchor + one interval, as in the SQLite test above
        assert job.last_completed_trigger_id == scheduled_trigger_id(
            job.job_id, START + timedelta(minutes=1)
        )
    assert "trading-company-customer-recovery" in capsys.readouterr().out


def test_composition_recovery_cli_rejects_policies_without_postgres(tmp_path, capsys):
    result = run_cli([
        "composition-recovery", "--sqlite", str(tmp_path / "s.sqlite"),
        "--resource-policies", str(tmp_path / "p.json"),
        "--owner-id", "cron",
        "--correlation-id", "test-customer-correlation",
        "--organization-id", "test-trading-organization",
        "--start-at", START.isoformat(),
        "--interval-seconds", "60",
    ])
    assert result == 2
    assert "--postgres-dsn" in capsys.readouterr().err
