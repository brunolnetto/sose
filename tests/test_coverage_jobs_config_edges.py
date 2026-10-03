from __future__ import annotations

import pytest

from sose.jobs.config import (
    CatalogJobSection,
    DomainSection,
    PersistenceSection,
    SOSECatalogConfig,
    JobSection,
    engine_namespace_for_job,
    load_sose_config,
)


def test_engine_namespace_prefixes_invalid_normalized_stem():
    namespace = engine_namespace_for_job("123 invalid id")
    assert namespace.startswith("job_")


def test_job_section_rejects_ticks_above_maximum():
    with pytest.raises(ValueError, match="cannot exceed"):
        JobSection(id="job", ticks_per_trigger=5, max_ticks_per_trigger=4)


def test_load_sose_config_reads_minimal_valid_document(tmp_path):
    path = tmp_path / "sose.toml"
    path.write_text(
        """
[domain]
name = "producer_consumer"

[job]
id = "demo"
""".strip(),
        encoding="utf-8",
    )

    config, base_dir = load_sose_config(path)
    assert config.domain.name == "producer_consumer"
    assert config.job.id == "demo"
    assert base_dir == tmp_path


def test_catalog_job_section_rejects_invalid_inputs():
    with pytest.raises(ValueError, match="cannot be empty"):
        CatalogJobSection(
            id="",
            domain=DomainSection(name="producer_consumer"),
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        CatalogJobSection(
            id="job",
            domain=DomainSection(name="producer_consumer"),
            ticks_per_trigger=4,
            max_ticks_per_trigger=3,
        )
    with pytest.raises(ValueError, match="SQL-safe identifier"):
        CatalogJobSection(
            id="job",
            domain=DomainSection(name="producer_consumer"),
            engine_namespace="invalid-name!",
        )


def test_catalog_config_rejects_duplicate_ids_and_namespaces():
    with pytest.raises(ValueError, match="ids must be unique"):
        SOSECatalogConfig(
            engine_store=PersistenceSection(
                adapter="sqlite_incremental",
                options={"path": "engine.sqlite3"},
            ),
            jobs=[
                CatalogJobSection(
                    id="dup",
                    domain=DomainSection(name="producer_consumer"),
                ),
                CatalogJobSection(
                    id="dup",
                    domain=DomainSection(name="sleeping_barber"),
                ),
            ],
        )

    with pytest.raises(ValueError, match="namespaces must be unique"):
        SOSECatalogConfig(
            engine_store=PersistenceSection(
                adapter="sqlite_incremental",
                options={"path": "engine.sqlite3"},
            ),
            jobs=[
                CatalogJobSection(
                    id="job-a",
                    domain=DomainSection(name="producer_consumer"),
                    engine_namespace="shared",
                ),
                CatalogJobSection(
                    id="job-b",
                    domain=DomainSection(name="sleeping_barber"),
                    engine_namespace="shared",
                ),
            ],
        )


def test_catalog_config_rejects_unsupported_adapter_and_reserved_namespace_option():
    with pytest.raises(ValueError, match="require 'sqlite_incremental' or 'postgres'"):
        SOSECatalogConfig(
            engine_store=PersistenceSection(adapter="memory"),
            jobs=[
                CatalogJobSection(
                    id="job-a",
                    domain=DomainSection(name="producer_consumer"),
                )
            ],
        )

    with pytest.raises(ValueError, match="namespace is job-specific"):
        SOSECatalogConfig(
            engine_store=PersistenceSection(
                adapter="sqlite_incremental",
                options={"path": "engine.sqlite3", "namespace": "bad"},
            ),
            jobs=[
                CatalogJobSection(
                    id="job-a",
                    domain=DomainSection(name="producer_consumer"),
                )
            ],
        )


def test_catalog_config_allows_postgres_without_sqlite_lowercase_constraint():
    config = SOSECatalogConfig(
        engine_store=PersistenceSection(
            adapter="postgres",
            options={"dsn": "postgresql://example"},
        ),
        jobs=[
            CatalogJobSection(
                id="job-a",
                domain=DomainSection(name="producer_consumer"),
                engine_namespace="MixedCaseNamespace",
            )
        ],
    )

    assert config.engine_store.adapter == "postgres"
