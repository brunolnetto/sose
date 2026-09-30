from __future__ import annotations

import json

from sose.cli import run_cli
from sose.jobs.factory import build_job_from_file


def _stdout_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_mro_declarative_recurring_job_survives_reopen_and_runtime_config_change(
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
            "plant-maintenance",
            "--persistence",
            "sqlite_incremental",
            "--persistence-path",
            "state/mro.sqlite3",
        ]
    ) == 0
    capsys.readouterr()

    assert run_cli(
        ["config", "set", "quantity", "2.0", "--config", str(config)]
    ) == 0
    capsys.readouterr()
    assert run_cli(
        [
            "config",
            "set",
            "auto_seed_spare_parts",
            "false",
            "--config",
            str(config),
        ]
    ) == 0
    capsys.readouterr()

    assert run_cli(
        [
            "storage",
            "add-sink",
            "audit",
            "jsonl",
            "--option",
            "path=analytics/events.jsonl",
            "--config",
            str(config),
        ]
    ) == 0
    capsys.readouterr()

    assert run_cli(["validate", "--config", str(config)]) == 0
    validated = _stdout_json(capsys)
    assert validated["domain"] == "mro"
    assert validated["persistence"] == "sqlite_incremental"
    assert validated["sinks"] == [{"adapter": "jsonl", "name": "audit"}]

    assert run_cli(
        [
            "trigger",
            "--config",
            str(config),
            "--scheduled-for",
            "2026-03-01T09:00:00Z",
        ]
    ) == 0
    first = _stdout_json(capsys)
    assert first["end_tick"] == 1
    assert first["config_revision"] == 1

    first_process = build_job_from_file(config)
    first_state = first_process.state()
    work_order_id = first_state.bootstrap_state.work_order_id
    work_order = first_process.persistence.entity("work_order", work_order_id)
    assert work_order is not None
    assert work_order.state == "waiting_material"
    close = getattr(first_process.persistence, "close", None)
    if callable(close):
        close()

    assert run_cli(
        [
            "config",
            "set",
            "auto_seed_spare_parts",
            "true",
            "--config",
            str(config),
        ]
    ) == 0
    capsys.readouterr()
    assert run_cli(["apply", "--config", str(config)]) == 0
    applied = _stdout_json(capsys)
    assert applied["changed"] is True
    assert applied["config_revision"] == 2

    assert run_cli(
        [
            "trigger",
            "--config",
            str(config),
            "--scheduled-for",
            "2026-03-01T10:00:00Z",
        ]
    ) == 0
    second = _stdout_json(capsys)
    assert second["start_tick"] == 1
    assert second["end_tick"] == 2
    assert second["config_revision"] == 2

    second_process = build_job_from_file(config)
    second_state = second_process.state()
    assert second_state.next_tick == 2
    assert second_state.config_revision == 2
    assert second_state.bootstrap_state.work_order_id == work_order_id

    same_work_order = second_process.persistence.entity(
        "work_order",
        work_order_id,
    )
    assert same_work_order is not None
    assert same_work_order.state == "in_progress"

    sink_path = tmp_path / "analytics" / "events.jsonl"
    assert sink_path.exists()
    assert sink_path.read_text(encoding="utf-8").strip()

    close = getattr(second_process.persistence, "close", None)
    if callable(close):
        close()


def test_completed_scheduler_occurrence_is_idempotent_across_processes(
    tmp_path,
    capsys,
):
    config = tmp_path / "sose.toml"
    assert run_cli(
        [
            "init",
            "--domain",
            "tutorial_job",
            "--output",
            str(config),
            "--job-id",
            "scheduler-idempotency",
            "--persistence",
            "sqlite_incremental",
            "--persistence-path",
            "state.sqlite3",
        ]
    ) == 0
    capsys.readouterr()

    args = [
        "trigger",
        "--config",
        str(config),
        "--scheduled-for",
        "2026-04-01T10:00:00Z",
    ]
    assert run_cli(args) == 0
    first = _stdout_json(capsys)

    # run_cli constructs a fresh job/persistence instance on every invocation.
    assert run_cli(args) == 0
    repeated = _stdout_json(capsys)

    assert repeated == first

    job = build_job_from_file(config)
    assert job.state().next_tick == first["end_tick"]
    close = getattr(job.persistence, "close", None)
    if callable(close):
        close()
