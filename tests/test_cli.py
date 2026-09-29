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
complete_after = "PT3H"

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


def test_cli_apply_rejects_bootstrap_only_change_after_initialization(tmp_path, capsys):
    config = _write(
        tmp_path / "bootstrap.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
complete_after = "PT3H"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "bootstrap-state.sqlite3"

[job]
id = "apply-bootstrap-cli"
""".strip(),
    )

    assert run_cli(
        ["run", "--config", str(config), "--trigger-id", "bootstrap-run-1"]
    ) == 0
    capsys.readouterr()

    _write(
        config,
        """
[domain]
name = "tutorial_job"

[domain.parameters]
complete_after = "PT1H"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "bootstrap-state.sqlite3"

[job]
id = "apply-bootstrap-cli"
""".strip(),
    )

    assert run_cli(["apply", "--config", str(config)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bootstrap-only" in captured.err
    assert "complete_after" in captured.err

    assert run_cli(["inspect", "--config", str(config)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["job"]["config_revision"] == 1
    assert inspected["config"]["complete_after"] == "PT3H"


def test_cli_lists_analytical_sink_adapters(capsys):
    assert run_cli(["sinks"]) == 0
    payload = json.loads(capsys.readouterr().out)

    adapters = {item["name"]: item for item in payload["adapters"]}
    assert set(adapters) == {"databricks", "jsonl", "snowflake"}
    assert adapters["databricks"]["optional_extra"] == "databricks"
    assert adapters["snowflake"]["optional_extra"] == "snowflake"


def test_cli_validate_rejects_unknown_analytical_sink(tmp_path, capsys):
    config = tmp_path / "sose.toml"
    config.write_text(
        """
[domain]
name = "tutorial_job"

[[sinks]]
name = "warehouse"
adapter = "unknown"

[job]
id = "sink-validation"
""".strip(),
        encoding="utf-8",
    )

    assert run_cli(["validate", "--config", str(config)]) == 2
    captured = capsys.readouterr()
    assert "unknown sink adapter" in captured.err


def test_cli_describes_domain_parameters(capsys):
    assert run_cli(["domains", "--name", "mro"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["name"] == "mro"
    assert payload["config_model"] == "MROConfig"
    assert payload["defaults"]["quantity"] == 1.0
    by_name = {item["name"]: item for item in payload["parameters"]}
    assert by_name["quantity"]["schema"]["exclusiveMinimum"] == 0
    assert by_name["technician_capacity"]["schema"]["minimum"] == 1


def test_cli_domain_parameter_discovery_rejects_unknown_domain(capsys):
    assert run_cli(["domains", "--name", "does-not-exist"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown domain" in captured.err



def test_cli_config_show_reports_effective_values_and_mutability(tmp_path, capsys):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
random_seed = 99
complete_after = "PT3H"

[job]
id = "config-show"
""".strip(),
    )

    assert run_cli(["config", "show", "--config", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    by_name = {item["name"]: item for item in payload["parameters"]}

    assert payload["domain"] == "tutorial_job"
    assert by_name["random_seed"]["value"] == 99
    assert by_name["random_seed"]["mutability"] == "runtime"
    assert by_name["complete_after"]["mutability"] == "bootstrap"
    assert by_name["auto_complete"]["value"] is True


def test_cli_config_set_validates_and_edits_runtime_parameter(tmp_path, capsys):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
random_seed = 1
complete_after = "PT3H"

[job]
id = "config-set"
""".strip(),
    )

    assert run_cli(
        [
            "config",
            "set",
            "random_seed",
            "99",
            "--config",
            str(config),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["name"] == "random_seed"
    assert payload["value"] == 99
    assert payload["mutability"] == "runtime"
    assert payload["applied"] is False

    assert run_cli(["validate", "--config", str(config)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["domain_parameters"]["random_seed"] == 99


def test_cli_config_set_can_add_missing_domain_parameters_section(tmp_path, capsys):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[job]
id = "config-insert"
""".strip(),
    )

    assert run_cli(
        [
            "config",
            "set",
            "auto_complete",
            "false",
            "--config",
            str(config),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["value"] is False
    assert "[domain.parameters]" in config.read_text(encoding="utf-8")

    assert run_cli(["validate", "--config", str(config)]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["domain_parameters"]["auto_complete"] is False


def test_cli_config_set_rejects_unknown_parameter_without_modifying_file(
    tmp_path,
    capsys,
):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[job]
id = "config-unknown"
""".strip(),
    )
    before = config.read_text(encoding="utf-8")

    assert run_cli(
        [
            "config",
            "set",
            "does_not_exist",
            "1",
            "--config",
            str(config),
        ]
    ) == 2
    captured = capsys.readouterr()
    assert "unknown domain parameter" in captured.err
    assert config.read_text(encoding="utf-8") == before


def test_cli_config_set_rejects_invalid_value_before_writing(tmp_path, capsys):
    config = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "mro"

[domain.parameters]
technician_capacity = 2

[job]
id = "config-invalid"
""".strip(),
    )
    before = config.read_text(encoding="utf-8")

    assert run_cli(
        [
            "config",
            "set",
            "technician_capacity",
            "0",
            "--config",
            str(config),
        ]
    ) == 2
    captured = capsys.readouterr()
    assert "greater than or equal to 1" in captured.err
    assert config.read_text(encoding="utf-8") == before


def test_cli_config_set_bootstrap_field_edits_file_but_apply_still_owns_runtime_guard(
    tmp_path,
    capsys,
):
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
path = "config-bootstrap.sqlite3"

[job]
id = "config-bootstrap"
""".strip(),
    )

    assert run_cli(
        ["run", "--config", str(config), "--trigger-id", "config-bootstrap-1"]
    ) == 0
    capsys.readouterr()

    assert run_cli(
        [
            "config",
            "set",
            "complete_after",
            "PT1H",
            "--config",
            str(config),
        ]
    ) == 0
    edited = json.loads(capsys.readouterr().out)
    assert edited["mutability"] == "bootstrap"
    assert edited["applied"] is False

    assert run_cli(["apply", "--config", str(config)]) == 2
    captured = capsys.readouterr()
    assert "bootstrap-only" in captured.err
