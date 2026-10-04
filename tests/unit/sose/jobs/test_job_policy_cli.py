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

[persistence.options]
path = "state.sqlite3"

[job]
id = "policy-job"
ticks_per_trigger = 2
max_ticks_per_trigger = 5
""".strip(),
        encoding="utf-8",
    )
    return path


def test_job_show_reports_recurring_policy(tmp_path, capsys):
    path = _write_config(tmp_path / "sose.toml")

    assert run_cli(["job", "show", "--config", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "job_id": "policy-job",
        "ticks_per_trigger": 2,
        "max_ticks_per_trigger": 5,
        "execution_model": "durable_recurring_trigger",
        "external_scheduler_owned": True,
    }


def test_job_set_policy_updates_partial_value_and_preserves_other_bound(
    tmp_path,
    capsys,
):
    path = _write_config(tmp_path / "sose.toml")

    assert run_cli(
        [
            "job",
            "set-policy",
            "--config",
            str(path),
            "--ticks-per-trigger",
            "4",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ticks_per_trigger"] == 4
    assert payload["max_ticks_per_trigger"] == 5

    config, _ = load_sose_config(path)
    assert config.job.ticks_per_trigger == 4
    assert config.job.max_ticks_per_trigger == 5


def test_invalid_job_policy_does_not_mutate_toml(tmp_path, capsys):
    path = _write_config(tmp_path / "sose.toml")
    before = path.read_text(encoding="utf-8")

    assert run_cli(
        [
            "job",
            "set-policy",
            "--config",
            str(path),
            "--ticks-per-trigger",
            "7",
        ]
    ) == 2

    assert "ticks_per_trigger" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before


def test_policy_edit_preserves_other_sections(tmp_path, capsys):
    path = _write_config(tmp_path / "sose.toml")
    before = path.read_text(encoding="utf-8")

    assert run_cli(
        [
            "job",
            "set-policy",
            "--config",
            str(path),
            "--max-ticks-per-trigger",
            "9",
        ]
    ) == 0
    capsys.readouterr()

    after = path.read_text(encoding="utf-8")
    assert '[domain]\nname = "tutorial_job"' in after
    assert 'adapter = "sqlite_incremental"' in after
    assert 'path = "state.sqlite3"' in after
    assert before != after


def test_trigger_uses_policy_edited_in_toml(tmp_path, capsys):
    path = _write_config(tmp_path / "sose.toml")

    assert run_cli(
        [
            "job",
            "set-policy",
            "--config",
            str(path),
            "--ticks-per-trigger",
            "3",
        ]
    ) == 0
    capsys.readouterr()

    assert run_cli(
        [
            "trigger",
            "--config",
            str(path),
            "--trigger-id",
            "scheduler-batch-1",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["requested_ticks"] == 3
    assert result["completed_ticks"] == 3
    assert result["start_tick"] == 0
    assert result["end_tick"] == 3
