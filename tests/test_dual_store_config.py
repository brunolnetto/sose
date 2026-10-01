from pathlib import Path

import pytest

from sose.jobs.config import SOSEConfig
from sose.jobs.factory import _domain_warehouse
from sose.domain.sqlite import SQLiteDomainWarehouse


def test_dual_store_config_builds_sqlite_domain_warehouse(tmp_path):
    config = SOSEConfig.model_validate({
        "domain": {"name": "tutorial_job"},
        "persistence": {"adapter": "sqlite_incremental", "options": {"path": "state/engine.db"}},
        "domain_warehouse": {"adapter": "sqlite", "options": {"path": "state/domain.db"}},
        "job": {"id": "dual-store"},
    })
    warehouse = _domain_warehouse(config, Path(tmp_path))
    assert isinstance(warehouse, SQLiteDomainWarehouse)
    assert warehouse.path == str(tmp_path / "state/domain.db")
    warehouse.close()


def test_public_factory_wires_domain_warehouse(tmp_path):
    from sose.jobs.factory import build_job_from_config

    config = SOSEConfig.model_validate({
        "domain": {"name": "tutorial_job"},
        "persistence": {
            "adapter": "sqlite_incremental",
            "options": {"path": "state/engine.db"},
        },
        "domain_warehouse": {
            "adapter": "sqlite",
            "options": {"path": "state/domain.db"},
        },
        "job": {"id": "dual-store-public"},
    })
    job = build_job_from_config(config, base_dir=Path(tmp_path))
    try:
        assert isinstance(job.domain_warehouse, SQLiteDomainWarehouse)
        assert job.persistence.entities() == ()
        assert job.domain_warehouse.entities()
    finally:
        job.close()


def test_sqlite_domain_warehouse_rejects_non_string_path(tmp_path):
    config = SOSEConfig.model_validate({
        "domain": {"name": "tutorial_job"},
        "domain_warehouse": {"adapter": "sqlite", "options": {"path": False}},
        "job": {"id": "invalid-path"},
    })
    with pytest.raises(ValueError, match="non-empty string"):
        _domain_warehouse(config, Path(tmp_path))
