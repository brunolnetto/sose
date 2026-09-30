from pathlib import Path

import pytest

from sose.cli import run_cli
from sose.jobs.config import SOSEConfig
from sose.jobs.storage import build_storage_plan


def _config(**overrides):
    payload = {
        "domain": {"name": "tutorial_job"},
        "persistence": {
            "adapter": "sqlite_incremental",
            "require": ["process_durable", "transactional_commits"],
            "options": {"path": "state.sqlite3"},
        },
        "sinks": [
            {
                "name": "warehouse",
                "adapter": "jsonl",
                "options": {"path": "analytics.jsonl"},
            }
        ],
        "job": {"id": "storage-plan"},
    }
    payload.update(overrides)
    return SOSEConfig.model_validate(payload)


def test_storage_plan_classifies_authoritative_and_analytical_roles():
    plan = build_storage_plan(_config())

    assert plan.healthy
    assert plan.authoritative.adapter.name == "sqlite_incremental"
    assert plan.authoritative.durable_recurring_ready
    assert "process_durable" in plan.authoritative.capabilities
    assert [sink.name for sink in plan.analytical] == ["warehouse"]
    assert [sink.adapter.name for sink in plan.analytical] == ["jsonl"]
    assert plan.issues == ()


def test_storage_plan_warns_when_authoritative_store_is_not_process_durable():
    config = _config(
        persistence={
            "adapter": "memory",
            "options": {},
        }
    )

    plan = build_storage_plan(config)

    assert plan.healthy
    assert not plan.authoritative.durable_recurring_ready
    assert [issue.code for issue in plan.issues] == [
        "storage.authoritative_not_process_durable"
    ]


def test_storage_plan_rejects_duplicate_analytical_names():
    config = _config(
        sinks=[
            {"name": "warehouse", "adapter": "jsonl"},
            {"name": "warehouse", "adapter": "jsonl"},
        ]
    )

    with pytest.raises(ValueError, match="duplicate analytical sink name"):
        build_storage_plan(config)


def test_storage_plan_enforces_required_authoritative_capabilities():
    config = _config(
        persistence={
            "adapter": "sqlite_incremental",
            "require": ["remote"],
        }
    )

    with pytest.raises(ValueError, match="lacks required capabilities"):
        build_storage_plan(config)


def test_cli_storage_prints_roles_without_opening_persistence(tmp_path, capsys):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "tutorial_job"

[persistence]
adapter = "sqlite_incremental"
require = ["process_durable", "transactional_commits"]

[persistence.options]
path = "state.sqlite3"

[[sinks]]
name = "warehouse"
adapter = "jsonl"

[sinks.options]
path = "analytics.jsonl"

[job]
id = "storage-cli"
""".strip(),
        encoding="utf-8",
    )

    assert run_cli(["storage", "--config", str(path)]) == 0

    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["authoritative"]["role"] == "authoritative"
    assert payload["authoritative"]["adapter"] == "sqlite_incremental"
    assert payload["authoritative"]["durable_recurring_ready"] is True
    assert payload["analytical"] == [
        {
            "role": "analytical",
            "name": "warehouse",
            "adapter": "jsonl",
            "optional_extra": None,
        }
    ]

    # Storage planning is read-only and should not instantiate/create the DB.
    assert not (tmp_path / "state.sqlite3").exists()


def test_build_job_uses_storage_plan_for_authoritative_and_sinks(tmp_path):
    from sose.jobs.factory import build_job_from_file

    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "tutorial_job"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[[sinks]]
name = "warehouse"
adapter = "jsonl"

[sinks.options]
path = "analytics.jsonl"

[job]
id = "storage-factory"
""".strip(),
        encoding="utf-8",
    )

    job = build_job_from_file(path)
    try:
        assert Path(job.persistence.path) == tmp_path / "state.sqlite3"
        assert [binding.name for binding in job.sink_bindings] == ["warehouse"]
    finally:
        job.persistence.close()
