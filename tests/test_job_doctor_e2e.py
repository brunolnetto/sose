from __future__ import annotations

import json
from pathlib import Path

from sose.cli import run_cli
from sose.jobs.doctor import inspect_job_file_health


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_doctor_reports_fresh_valid_job_as_healthy(tmp_path):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
complete_after = "PT2H"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "doctor-fresh"
""".strip(),
    )

    report = inspect_job_file_health(config)

    assert report.healthy
    assert report.initialized is False
    assert report.config_revision is None
    assert report.logical_tick is None
    assert report.issues == ()


def test_doctor_detects_declarative_config_drift_without_mutation(tmp_path):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
random_seed = 11
complete_after = "PT3H"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "doctor-drift"
""".strip(),
    )

    assert run_cli(["run", "--config", str(config), "--trigger-id", "run-1"]) == 0

    _write(
        config,
        """
[domain]
name = "tutorial_job"

[domain.parameters]
random_seed = 99
complete_after = "PT1H"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "doctor-drift"
""".strip(),
    )

    report = inspect_job_file_health(config)

    assert not report.healthy
    assert [issue.code for issue in report.issues] == ["job.config_drift"]
    assert report.config_revision == 1

    # Read-only: the durable revision remains unchanged.
    assert run_cli(["inspect", "--config", str(config)]) == 0


def test_cli_doctor_returns_nonzero_for_drift_and_zero_after_apply(tmp_path, capsys):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
random_seed = 1

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "doctor-cli"
""".strip(),
    )

    assert run_cli(["run", "--config", str(config), "--trigger-id", "first"]) == 0
    capsys.readouterr()

    _write(
        config,
        """
[domain]
name = "tutorial_job"

[domain.parameters]
random_seed = 2

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "doctor-cli"
""".strip(),
    )

    assert run_cli(["doctor", "--config", str(config)]) == 1
    unhealthy = json.loads(capsys.readouterr().out)
    assert unhealthy["healthy"] is False
    assert unhealthy["issues"][0]["code"] == "job.config_drift"

    assert run_cli(["apply", "--config", str(config)]) == 0
    capsys.readouterr()

    assert run_cli(["doctor", "--config", str(config)]) == 0
    healthy = json.loads(capsys.readouterr().out)
    assert healthy["healthy"] is True
    assert healthy["config_revision"] == 2


def test_scaffolded_mro_product_flow_init_tick_inspect_apply_reopen_tick(
    tmp_path,
    capsys,
):
    config = tmp_path / "sose.toml"

    assert run_cli(
        [
            "init",
            "--domain",
            "mro",
            "--output",
            str(config),
            "--job-id",
            "mro-product",
        ]
    ) == 0
    capsys.readouterr()

    assert run_cli(["validate", "--config", str(config)]) == 0
    capsys.readouterr()

    assert run_cli(["doctor", "--config", str(config)]) == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial["initialized"] is False

    assert run_cli(
        ["run", "--config", str(config), "--trigger-id", "scheduler-001"]
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["logical_tick"] == 1

    assert run_cli(["inspect", "--config", str(config)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["job"]["next_tick"] == 1
    assert inspected["job"]["run_count"] == 1

    assert run_cli(["doctor", "--config", str(config)]) == 0
    healthy = json.loads(capsys.readouterr().out)
    assert healthy["healthy"] is True
    assert healthy["logical_tick"] == 1

    # A second CLI invocation opens a fresh adapter/process-equivalent job and
    # advances exactly one more durable tick.
    assert run_cli(
        ["run", "--config", str(config), "--trigger-id", "scheduler-002"]
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["logical_tick"] == 2
    assert second["run_count"] == 2
