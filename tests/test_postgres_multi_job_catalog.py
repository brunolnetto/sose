from __future__ import annotations

import os
from uuid import uuid4

import pytest

from sose.jobs.catalog import build_job_catalog_from_config
from sose.jobs.config import (
    CatalogJobSection,
    DomainSection,
    DomainWarehouseSection,
    PersistenceSection,
    SOSECatalogConfig,
)


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="SOSE_TEST_POSTGRES_DSN is required",
)


def test_multiple_jobs_share_one_postgres_database_with_independent_warehouses(
    tmp_path,
):
    assert DSN is not None
    suffix = uuid4().hex[:8]
    config = SOSECatalogConfig(
        engine_store=PersistenceSection(
            adapter="postgres",
            options={"dsn": DSN},
        ),
        jobs=[
            CatalogJobSection(
                id=f"orders-{suffix}",
                engine_namespace=f"orders_{suffix}",
                domain=DomainSection(name="order_to_cash"),
                domain_store=DomainWarehouseSection(
                    adapter="sqlite",
                    options={"path": f"orders-{suffix}.sqlite3"},
                ),
            ),
            CatalogJobSection(
                id=f"maintenance-{suffix}",
                engine_namespace=f"maintenance_{suffix}",
                domain=DomainSection(name="mro"),
                domain_store=DomainWarehouseSection(
                    adapter="sqlite",
                    options={"path": f"maintenance-{suffix}.sqlite3"},
                ),
            ),
        ],
    )

    catalog = build_job_catalog_from_config(config, base_dir=tmp_path)
    try:
        orders = catalog.get(f"orders-{suffix}")
        maintenance = catalog.get(f"maintenance-{suffix}")

        assert orders.persistence.dsn == maintenance.persistence.dsn == DSN
        assert orders.persistence.namespace != maintenance.persistence.namespace
        assert len(orders.persistence.job_states()) == 1
        assert len(maintenance.persistence.job_states()) == 1

        orders.run_tick(trigger_id="orders:1")
        maintenance.run_tick(trigger_id="maintenance:1")

        assert orders.state().next_tick == 1
        assert maintenance.state().next_tick == 1
        assert orders.domain_warehouse is not maintenance.domain_warehouse

        orders.run_tick(trigger_id="orders:2")
        assert orders.state().next_tick == 2
        assert maintenance.state().next_tick == 1
    finally:
        catalog.close()
