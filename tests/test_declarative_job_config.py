from pathlib import Path

import pytest

from sose.jobs.config import load_sose_config
from sose.jobs.factory import build_job_from_file
from sose.persistence.registry import builtin_persistence_registry


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_builtin_persistence_registry_exposes_local_sinks(tmp_path):
    registry = builtin_persistence_registry()

    assert registry.names() == (
        "duckdb",
        "jsonl",
        "memory",
        "postgres",
        "sqlite",
        "sqlite_incremental",
    )

    persistence = registry.create(
        "sqlite_incremental",
        {"path": "state/job.sqlite3"},
        base_dir=tmp_path,
    )
    assert persistence.path == str(tmp_path / "state/job.sqlite3")
    persistence.close()


def test_sose_toml_loads_domain_persistence_runtime_and_job(tmp_path):
    path = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "mro"

[domain.parameters]
quantity = 2.0
technician_capacity = 2
maintenance_bay_capacity = 2
spare_part_store_capacity = 20
release_delay = "PT1H"
tick_step = "PT30M"
random_seed = 77

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state/mro.sqlite3"

[runtime]
backend = "simpy"

[job]
id = "mro-daily"
""".strip(),
    )

    config, base_dir = load_sose_config(path)

    assert base_dir == tmp_path
    assert config.domain.name == "mro"
    assert config.domain.parameters["quantity"] == 2.0
    assert config.persistence.adapter == "sqlite_incremental"
    assert config.persistence.options["path"] == "state/mro.sqlite3"
    assert config.runtime.backend == "simpy"
    assert config.job.id == "mro-daily"


def test_declarative_mro_job_advances_one_tick_and_reopens(tmp_path):
    path = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "mro"

[domain.parameters]
quantity = 2.0
technician_capacity = 2
maintenance_bay_capacity = 2
release_delay = "PT1H"

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state/mro.sqlite3"

[runtime]
backend = "simpy"

[job]
id = "mro-recurring"
""".strip(),
    )

    first_job = build_job_from_file(path)
    first = first_job.run_tick(trigger_id="scheduler-001")
    first_job.persistence.close()

    resumed_job = build_job_from_file(path)
    second = resumed_job.run_tick(trigger_id="scheduler-002")

    assert first.logical_tick == 1
    assert second.logical_tick == 2
    assert second.run_count == 2
    assert resumed_job.state().domain_name == "mro"
    assert resumed_job.state().config_revision == 1
    resumed_job.persistence.close()


def test_existing_job_ignores_new_file_parameters_until_explicit_update(tmp_path):
    path = _write(
        tmp_path / "sose.toml",
        """
[domain]
name = "tutorial_job"

[domain.parameters]
complete_after = "PT3H"
random_seed = 1

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "stable-config"
""".strip(),
    )
    first_job = build_job_from_file(path)
    first_job.run_tick(trigger_id="one")
    persisted_revision = first_job.state().config_revision
    first_job.persistence.close()

    _write(
        path,
        """
[domain]
name = "tutorial_job"

[domain.parameters]
complete_after = "PT1H"
random_seed = 999

[persistence]
adapter = "sqlite_incremental"

[persistence.options]
path = "state.sqlite3"

[job]
id = "stable-config"
""".strip(),
    )

    reopened = build_job_from_file(path)
    assert reopened.state().config_revision == persisted_revision
    reopened.persistence.close()


def test_unknown_domain_and_backend_fail_explicitly(tmp_path):
    unknown_domain = _write(
        tmp_path / "unknown-domain.toml",
        """
[domain]
name = "does_not_exist"

[job]
id = "bad-domain"
""".strip(),
    )
    with pytest.raises(KeyError, match="unknown domain"):
        build_job_from_file(unknown_domain)

    unknown_backend = _write(
        tmp_path / "unknown-backend.toml",
        """
[domain]
name = "tutorial_job"

[runtime]
backend = "does_not_exist"

[job]
id = "bad-backend"
""".strip(),
    )
    with pytest.raises(KeyError, match="unknown runtime backend"):
        build_job_from_file(unknown_backend)
