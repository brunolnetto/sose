"""Public CLI drives the same durable catch-up protocol across process restarts."""
from datetime import datetime, timedelta, timezone

from sose.cli import run_cli
from sose.jobs.runner import scheduled_trigger_id
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


START = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def test_composition_recovery_one_shot_cron_invocations(tmp_path, capsys):
    db = tmp_path / "composition.sqlite"
    args = [
        "composition-recovery", "--sqlite", str(db),
        "--owner-id", "cron", "--start-at", START.isoformat(),
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
        "--owner-id", "cron", "--start-at", "2026-10-09T12:00:00",
        "--interval-seconds", "60",
    ])
    assert result == 2
    assert "timezone" in capsys.readouterr().err
