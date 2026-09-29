import json
from pathlib import Path

from sose.cli import run_cli


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_cli_lists_domains_and_persistence(capsys):
    assert run_cli(["domains"]) == 0
    domains = json.loads(capsys.readouterr().out)
    assert any(item["name"] == "mro" for item in domains)

    assert run_cli(["persistence"]) == 0
    adapters = json.loads(capsys.readouterr().out)
    by_name = {item["name"]: item for item in adapters["adapters"]}
    assert "sqlite_incremental" in by_name
    assert "process_durable" in by_name["sqlite_incremental"]["capabilities"]
    assert "incremental_updates" in by_name["sqlite_incremental"]["capabilities"]
    assert "analytical_reads" in by_name["duckdb"]["capabilities"]
    assert "remote" in by_name["postgres"]["capabilities"]
    assert "concurrent_writers" in by_name["postgres"]["capabilities"]


def test_cli_validate_and_run_one_tick(tmp_path, capsys):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
complete_after = "PT2H"
tick_step = "PT1H"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[runtime]
backend = "simpy"

[job]
id = "cli-job"
""".strip(),
    )

    assert run_cli(["validate", "--config", str(config)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["domain"] == "tutorial_job"
    assert validated["job_id"] == "cli-job"

    assert run_cli(
        [
            "run",
            "--config",
            str(config),
            "--trigger-id",
            "scheduler-001",
        ]
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["logical_tick"] == 1
    assert first["run_count"] == 1
    assert first["trigger_id"] == "scheduler-001"


def test_cli_inspect_is_read_only_and_next_run_resumes(tmp_path, capsys):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
complete_after = "PT3H"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "inspect-job"
""".strip(),
    )

    assert run_cli(
        ["run", "--config", str(config), "--trigger-id", "run-1"]
    ) == 0
    capsys.readouterr()

    assert run_cli(["inspect", "--config", str(config)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["job"]["next_tick"] == 1
    assert inspected["job"]["run_count"] == 1
    assert inspected["position"]["logical_tick"] == 1

    assert run_cli(
        ["run", "--config", str(config), "--trigger-id", "run-2"]
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["logical_tick"] == 2
    assert second["run_count"] == 2


def test_cli_reports_validation_error_without_traceback(tmp_path, capsys):
    config = _write(
        tmp_path / "bad.toml",
        """
[domain]
name = "does_not_exist"

[job]
id = "bad"
""".strip(),
    )

    assert run_cli(["validate", "--config", str(config)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown domain" in captured.err


def test_cli_apply_updates_existing_job_revision_explicitly(tmp_path, capsys):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
random_seed = 1
complete_after = "PT3H"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "apply-cli"
""".strip(),
    )

    assert run_cli(["run", "--config", str(config), "--trigger-id", "run-1"]) == 0
    capsys.readouterr()

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
id = "apply-cli"
""".strip(),
    )

    assert run_cli(["apply", "--config", str(config)]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["changed"] is True
    assert applied["config_revision"] == 2
    assert applied["config"]["random_seed"] == 99

    assert run_cli(["apply", "--config", str(config)]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["changed"] is False
    assert repeated["config_revision"] == 2
