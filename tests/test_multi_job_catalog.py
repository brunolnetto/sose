from __future__ import annotations

import sqlite3

from sose.jobs.catalog import build_job_catalog_from_config
from sose.jobs.config import (
    CatalogJobSection,
    DomainSection,
    DomainWarehouseSection,
    PersistenceSection,
    SOSECatalogConfig,
    load_sose_catalog_config,
)


def _sqlite_catalog(tmp_path):
    return SOSECatalogConfig(
        engine_store=PersistenceSection(
            adapter="sqlite_incremental",
            options={"path": "shared-engine.sqlite3"},
        ),
        jobs=[
            CatalogJobSection(
                id="producer-east",
                domain=DomainSection(name="producer_consumer"),
                domain_store=DomainWarehouseSection(
                    adapter="sqlite",
                    options={"path": "producer-domain.sqlite3"},
                ),
            ),
            CatalogJobSection(
                id="barber-west",
                domain=DomainSection(name="sleeping_barber"),
                domain_store=DomainWarehouseSection(
                    adapter="sqlite",
                    options={"path": "barber-domain.sqlite3"},
                ),
            ),
        ],
    )


def test_multiple_jobs_share_one_sqlite_engine_file_without_state_leakage(tmp_path):
    config = _sqlite_catalog(tmp_path)
    catalog = build_job_catalog_from_config(config, base_dir=tmp_path)
    try:
        producer = catalog.get("producer-east")
        barber = catalog.get("barber-west")

        assert producer.persistence.path == barber.persistence.path
        assert producer.persistence.namespace != barber.persistence.namespace
        assert producer.persistence.job_states()[0].job_id == "producer-east"
        assert barber.persistence.job_states()[0].job_id == "barber-west"
        assert len(producer.persistence.job_states()) == 1
        assert len(barber.persistence.job_states()) == 1

        producer.run_tick(trigger_id="producer:1")
        barber.run_tick(trigger_id="barber:1")

        assert {d.name for d in producer.persistence.store_definitions()} >= {
            "__canonical_actions__",
            "buffer",
        }
        assert {d.name for d in barber.persistence.store_definitions()} >= {
            "__canonical_actions__",
            "abandoned",
        }
        assert producer.persistence.resource_definitions() == ()
        assert {d.name for d in barber.persistence.resource_definitions()} == {
            "barber"
        }

        producer_entities = producer.domain_warehouse.entities()
        barber_entities = barber.domain_warehouse.entities()
        assert len(producer_entities) == 1
        assert len(barber_entities) == 1
        assert producer_entities[0].attributes["canonical"] == "producer_consumer"
        assert barber_entities[0].attributes["canonical"] == "sleeping_barber"
    finally:
        catalog.close()

    database = tmp_path / "shared-engine.sqlite3"
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    for job in config.jobs:
        namespace = job.resolved_engine_namespace
        assert f"{namespace}_record_meta" in tables
        assert f"{namespace}_record" in tables

    reopened = build_job_catalog_from_config(config, base_dir=tmp_path)
    try:
        producer = reopened.get("producer-east")
        barber = reopened.get("barber-west")
        assert producer.state().next_tick == 1
        assert barber.state().next_tick == 1

        producer.run_tick(trigger_id="producer:2")
        assert producer.state().next_tick == 2
        assert barber.state().next_tick == 1
    finally:
        reopened.close()


def test_same_domain_can_run_as_independent_jobs_with_separate_warehouses(tmp_path):
    config = SOSECatalogConfig(
        engine_store=PersistenceSection(
            adapter="sqlite_incremental",
            options={"path": "same-domain-engine.sqlite3"},
        ),
        jobs=[
            CatalogJobSection(
                id="line-a",
                domain=DomainSection(name="producer_consumer"),
                domain_store=DomainWarehouseSection(
                    adapter="sqlite",
                    options={"path": "line-a.sqlite3"},
                ),
            ),
            CatalogJobSection(
                id="line-b",
                domain=DomainSection(name="producer_consumer"),
                domain_store=DomainWarehouseSection(
                    adapter="sqlite",
                    options={"path": "line-b.sqlite3"},
                ),
            ),
        ],
    )
    catalog = build_job_catalog_from_config(config, base_dir=tmp_path)
    try:
        first = catalog.get("line-a")
        second = catalog.get("line-b")
        first.run_tick(trigger_id="line-a:1")

        assert first.state().next_tick == 1
        assert second.state().next_tick == 0
        assert len(first.persistence.store_items()) > 0
        assert second.persistence.store_items() == ()
        assert first.domain_warehouse is not second.domain_warehouse
    finally:
        catalog.close()



def test_catalog_toml_loads_shared_engine_and_per_job_domain_stores(tmp_path):
    path = tmp_path / "sose-catalog.toml"
    path.write_text(
        """
[engine_store]
adapter = "sqlite_incremental"

[engine_store.options]
path = "shared.sqlite3"

[[jobs]]
id = "producer-a"
engine_namespace = "producer_a"

[jobs.domain]
name = "producer_consumer"

[jobs.domain_store]
adapter = "sqlite"

[jobs.domain_store.options]
path = "producer-a-domain.sqlite3"

[[jobs]]
id = "barber-b"
engine_namespace = "barber_b"

[jobs.domain]
name = "sleeping_barber"

[jobs.domain_store]
adapter = "sqlite"

[jobs.domain_store.options]
path = "barber-b-domain.sqlite3"
""".strip(),
        encoding="utf-8",
    )

    config, base_dir = load_sose_catalog_config(path)
    assert base_dir == tmp_path
    assert config.engine_store.options["path"] == "shared.sqlite3"
    assert [job.id for job in config.jobs] == ["producer-a", "barber-b"]
    assert config.jobs[0].domain.name == "producer_consumer"
    assert config.jobs[1].domain_store.options["path"] == "barber-b-domain.sqlite3"
