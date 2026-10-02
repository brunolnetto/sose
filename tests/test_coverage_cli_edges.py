from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from sose.cli import _close_persistence, main, run_cli


def _write_config(
    path: Path,
    *,
    runtime: str = "simpy",
    domain_store: str = "",
) -> Path:
    path.write_text(
        f"""
[domain]
name = "tutorial_job"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[runtime]
backend = "{runtime}"

[job]
id = "cli-edge"

{domain_store}
""".strip(),
        encoding="utf-8",
    )
    return path


def test_validate_rejects_unknown_runtime_backend(tmp_path, capsys):
    config = _write_config(tmp_path / "sose.toml", runtime="unknown")

    assert run_cli(["validate", "--config", str(config)]) == 2
    captured = capsys.readouterr()
    assert "unknown runtime backend" in captured.err


@pytest.mark.parametrize(
    ("domain_store", "message"),
    [
        (
            """
[domain_store]
adapter = "sqlite"

[domain_store.options]
path = ""
""",
            "sqlite DomainWarehouse path must be a non-empty string",
        ),
        (
            """
[domain_store]
adapter = "sqlite"

[domain_store.options]
path = 7
""",
            "sqlite DomainWarehouse path must be a non-empty string",
        ),
        (
            """
[domain_store]
adapter = "postgres"

[domain_store.options]
dsn = 7
""",
            "postgres DomainWarehouse dsn must be a non-empty string",
        ),
        (
            """
[domain_store]
adapter = "postgres"

[domain_store.options]
dsn_env = ""
""",
            "postgres DomainWarehouse dsn_env must be a non-empty string",
        ),
        (
            """
[domain_store]
adapter = "postgres"

[domain_store.options]
namespace = ""
""",
            "postgres DomainWarehouse namespace must be a non-empty string",
        ),
        (
            """
[domain_store]
adapter = "not-real"
""",
            "unknown DomainWarehouse adapter",
        ),
        (
            """
[domain_store]
adapter = "sqlite"

[domain_store.options]
unexpected = true
""",
            "unknown sqlite DomainWarehouse options",
        ),
        (
            """
[domain_store]
adapter = "postgres"

[domain_store.options]
dsn = "postgresql://example"
unexpected = true
""",
            "unknown postgres DomainWarehouse options",
        ),
    ],
)
def test_validate_rejects_invalid_domain_store_options(
    tmp_path, capsys, domain_store, message
):
    config = _write_config(
        tmp_path / "sose.toml",
        domain_store=domain_store,
    )

    assert run_cli(["validate", "--config", str(config)]) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "domain_store",
    [
        """
[domain_store]
adapter = "sqlite"
""",
        """
[domain_store]
adapter = "sqlite"

[domain_store.options]
path = "domain.sqlite3"
""",
        """
[domain_store]
adapter = "postgres"

[domain_store.options]
dsn = "postgresql://example"
namespace = "domain_ns"
""",
        """
[domain_store]
adapter = "postgres"

[domain_store.options]
dsn_env = "DOMAIN_DATABASE_URL"
""",
    ],
)
def test_validate_accepts_supported_domain_store_envelopes(
    tmp_path, capsys, domain_store
):
    config = _write_config(
        tmp_path / "sose.toml",
        domain_store=domain_store,
    )

    assert run_cli(["validate", "--config", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == "cli-edge"


def test_inspect_before_job_initialization_reports_empty_checkpoint(tmp_path, capsys):
    config = _write_config(tmp_path / "sose.toml")

    assert run_cli(["inspect", "--config", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"job": None, "position": None}


def test_close_persistence_accepts_objects_without_close():
    _close_persistence(object())


def test_main_propagates_run_cli_exit_code(monkeypatch):
    monkeypatch.setattr("sose.cli.run_cli", lambda: 37)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 37


def test_module_entrypoint_executes_cli():
    completed = subprocess.run(
        [sys.executable, "-m", "sose.cli", "persistence"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert "adapters" in payload
    assert completed.stderr == ""
