from __future__ import annotations

import json

from sose.cli import run_cli
from sose.jobs.plan import build_execution_plan_from_file


def test_execution_plan_resolves_complete_job_without_side_effects(tmp_path):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "mro"

[domain.parameters]
quantity = 2.0
auto_seed_spare_parts = false

[persistence]
adapter = "sqlite_incremental"
require = ["process_durable", "transactional_commits"]

[persistence.options]
path = "state/mro.sqlite3"

[[sinks]]
name = "lakehouse"
adapter = "databricks"

[sinks.options]
server_hostname_env = "DATABRICKS_SERVER_HOSTNAME"
http_path_env = "DATABRICKS_HTTP_PATH"
access_token_env = "DATABRICKS_TOKEN"

[runtime]
backend = "simpy"

[job]
id = "mro-plan"
ticks_per_trigger = 4
max_ticks_per_trigger = 20
""".strip(),
        encoding="utf-8",
    )

    plan = build_execution_plan_from_file(path)

    assert plan["domain"]["name"] == "mro"
    assert plan["domain"]["parameters"]["quantity"] == 2.0
    assert plan["domain"]["parameters"]["auto_seed_spare_parts"] is False
    assert "auto_seed_spare_parts" in plan["domain"]["runtime_mutable_fields"]

    assert plan["storage"]["authoritative"]["adapter"] == "sqlite_incremental"
    assert plan["storage"]["authoritative"]["durable_recurring_ready"] is True
    assert plan["storage"]["analytical"][0]["adapter"] == "databricks"
    assert plan["runtime"] == {"backend": "simpy"}
    assert plan["job"]["id"] == "mro-plan"
    assert plan["job"]["ticks_per_trigger"] == 4
    assert plan["job"]["max_ticks_per_trigger"] == 20

    # Planning validates metadata only; it must not instantiate the DB or sink.
    assert not (tmp_path / "state" / "mro.sqlite3").exists()


def test_execution_plan_can_resolve_postgres_without_credentials(tmp_path):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "tutorial_job"

[persistence]
adapter = "postgres"
require = ["remote", "concurrent_writers"]

[persistence.options]
dsn_env = "MISSING_FOR_PLAN_ONLY"
namespace = "tutorial_plan"

[job]
id = "postgres-plan"
""".strip(),
        encoding="utf-8",
    )

    plan = build_execution_plan_from_file(path)

    assert plan["storage"]["authoritative"]["adapter"] == "postgres"
    assert "remote" in plan["storage"]["authoritative"]["capabilities"]
    assert plan["storage"]["issues"] == []


def test_cli_plan_outputs_json_and_is_read_only(tmp_path, capsys):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "tutorial_job"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "cli-plan"
ticks_per_trigger = 3
max_ticks_per_trigger = 9
""".strip(),
        encoding="utf-8",
    )

    assert run_cli(["plan", "--config", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["domain"]["name"] == "tutorial_job"
    assert payload["storage"]["authoritative"]["role"] == "authoritative"
    assert payload["job"]["ticks_per_trigger"] == 3
    assert payload["job"]["external_scheduler_owned"] is True
    assert not (tmp_path / "state.sqlite3").exists()


def test_plan_surfaces_non_durable_authoritative_warning(tmp_path):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "tutorial_job"

[persistence]
adapter = "memory"

[job]
id = "ephemeral-plan"
""".strip(),
        encoding="utf-8",
    )

    plan = build_execution_plan_from_file(path)

    issues = plan["storage"]["issues"]
    assert issues[0]["code"] == "storage.authoritative_not_process_durable"
