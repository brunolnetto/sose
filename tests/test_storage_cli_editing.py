from __future__ import annotations

import json

from sose.cli import run_cli
from sose.jobs.config import load_sose_config


def _write_config(path):
    path.write_text(
        """
[domain]
name = "tutorial_job"

[persistence]
adapter = "sqlite_incremental"
require = ["process_durable", "transactional_commits"]

[persistence.options]
path = "state.sqlite3"

[runtime]
backend = "simpy"

[job]
id = "storage-edit"
ticks_per_trigger = 1
max_ticks_per_trigger = 10
""".strip(),
        encoding="utf-8",
    )
    return path


def test_cli_can_switch_authoritative_storage_without_opening_connection(
    tmp_path,
    capsys,
):
    path = _write_config(tmp_path / "sose.toml")

    result = run_cli(
        [
            "storage",
            "set-authoritative",
            "postgres",
            "--config",
            str(path),
            "--require",
            "process_durable",
            "--require",
            "transactional_commits",
            "--require",
            "concurrent_writers",
            "--require",
            "remote",
            "--option",
            "dsn_env=SOSE_DATABASE_URL",
            "--option",
            "namespace=storage_edit",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["adapter"] == "postgres"
    assert payload["applied"] is False

    config, _ = load_sose_config(path)
    assert config.persistence.adapter == "postgres"
    assert config.persistence.require == [
        "process_durable",
        "transactional_commits",
        "concurrent_writers",
        "remote",
    ]
    assert config.persistence.options == {
        "dsn_env": "SOSE_DATABASE_URL",
        "namespace": "storage_edit",
    }


def test_cli_can_add_and_remove_analytical_sink(tmp_path, capsys):
    path = _write_config(tmp_path / "sose.toml")

    assert run_cli(
        [
            "storage",
            "add-sink",
            "lakehouse",
            "jsonl",
            "--config",
            str(path),
            "--option",
            "path=analytics/events.jsonl",
        ]
    ) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["name"] == "lakehouse"
    assert added["adapter"] == "jsonl"

    config, _ = load_sose_config(path)
    assert len(config.sinks) == 1
    assert config.sinks[0].name == "lakehouse"
    assert config.sinks[0].options["path"] == "analytics/events.jsonl"

    assert run_cli(
        [
            "storage",
            "remove-sink",
            "lakehouse",
            "--config",
            str(path),
        ]
    ) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"] is True

    config, _ = load_sose_config(path)
    assert config.sinks == []


def test_storage_edit_rejects_unknown_adapter_without_mutating_file(
    tmp_path,
    capsys,
):
    path = _write_config(tmp_path / "sose.toml")
    before = path.read_text(encoding="utf-8")

    assert run_cli(
        [
            "storage",
            "set-authoritative",
            "does_not_exist",
            "--config",
            str(path),
        ]
    ) == 2

    assert "unknown persistence adapter" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before


def test_storage_edit_rejects_incompatible_requirement_without_mutation(
    tmp_path,
    capsys,
):
    path = _write_config(tmp_path / "sose.toml")
    before = path.read_text(encoding="utf-8")

    assert run_cli(
        [
            "storage",
            "set-authoritative",
            "sqlite_incremental",
            "--config",
            str(path),
            "--require",
            "remote",
        ]
    ) == 2

    assert "lacks required capabilities" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before


def test_storage_edit_rejects_duplicate_sink_without_mutation(
    tmp_path,
    capsys,
):
    path = _write_config(tmp_path / "sose.toml")

    assert run_cli(
        [
            "storage",
            "add-sink",
            "warehouse",
            "jsonl",
            "--config",
            str(path),
        ]
    ) == 0
    capsys.readouterr()
    before = path.read_text(encoding="utf-8")

    assert run_cli(
        [
            "storage",
            "add-sink",
            "warehouse",
            "jsonl",
            "--config",
            str(path),
        ]
    ) == 2

    assert "already exists" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before


def test_storage_option_parser_accepts_json_scalars(tmp_path, capsys):
    path = _write_config(tmp_path / "sose.toml")

    assert run_cli(
        [
            "storage",
            "add-sink",
            "warehouse",
            "jsonl",
            "--config",
            str(path),
            "--option",
            "path=analytics.jsonl",
        ]
    ) == 0
    capsys.readouterr()

    assert run_cli(["storage", "--config", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["analytical"][0]["name"] == "warehouse"
