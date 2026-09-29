from pathlib import Path

import pytest

from sose.jobs.config import SOSEConfig
from sose.jobs.factory import build_job_from_config
from sose.persistence.registry import (
    PersistenceCapabilities,
    builtin_persistence_registry,
)


def test_builtin_persistence_capabilities_are_explicit():
    registry = builtin_persistence_registry()

    memory = registry.capabilities("memory")
    assert memory.process_durable is False
    assert memory.transactional_commits is True

    snapshot = registry.capabilities("sqlite")
    assert snapshot.process_durable is True
    assert snapshot.incremental_updates is False
    assert snapshot.schema_migrations is True

    incremental = registry.capabilities("sqlite_incremental")
    assert incremental.process_durable is True
    assert incremental.incremental_updates is True
    assert incremental.concurrent_writers is False

    jsonl = registry.capabilities("jsonl")
    assert jsonl.append_only is True
    assert jsonl.analytical_reads is False

    duckdb = registry.capabilities("duckdb")
    assert duckdb.analytical_reads is True
    assert duckdb.remote is False

    postgres = registry.capabilities("postgres")
    assert postgres.process_durable is True
    assert postgres.incremental_updates is True
    assert postgres.concurrent_writers is True
    assert postgres.remote is True


def test_registry_require_accepts_supported_capabilities():
    registry = builtin_persistence_registry()

    adapter = registry.require(
        "sqlite_incremental",
        "process_durable",
        "transactional_commits",
        "incremental_updates",
    )

    assert adapter.name == "sqlite_incremental"


def test_registry_require_rejects_missing_or_unknown_capability():
    registry = builtin_persistence_registry()

    with pytest.raises(ValueError, match="concurrent_writers"):
        registry.require("sqlite_incremental", "concurrent_writers")

    with pytest.raises(ValueError, match="not_a_capability"):
        registry.require("sqlite_incremental", "not_a_capability")


def test_declarative_job_rejects_incompatible_persistence_requirements(tmp_path):
    config = SOSEConfig.model_validate(
        {
            "domain": {"name": "tutorial_job"},
            "persistence": {
                "adapter": "sqlite_incremental",
                "require": ["remote"],
                "options": {"path": "state.sqlite3"},
            },
            "job": {"id": "remote-required"},
        }
    )

    with pytest.raises(ValueError, match="remote"):
        build_job_from_config(config, base_dir=tmp_path)


def test_declarative_job_accepts_explicit_durability_requirements(tmp_path):
    config = SOSEConfig.model_validate(
        {
            "domain": {"name": "tutorial_job"},
            "persistence": {
                "adapter": "sqlite_incremental",
                "require": [
                    "process_durable",
                    "transactional_commits",
                    "incremental_updates",
                ],
                "options": {"path": "state.sqlite3"},
            },
            "job": {"id": "durable-required"},
        }
    )

    job = build_job_from_config(config, base_dir=tmp_path)
    try:
        assert job.persistence.path == str(tmp_path / "state.sqlite3")
    finally:
        job.persistence.close()


def test_capability_names_only_include_true_flags():
    capabilities = PersistenceCapabilities(
        process_durable=True,
        transactional_commits=True,
        incremental_updates=False,
        concurrent_writers=False,
        remote=True,
        analytical_reads=False,
        append_only=False,
        schema_migrations=True,
    )

    assert capabilities.names() == (
        "process_durable",
        "transactional_commits",
        "remote",
        "schema_migrations",
    )
