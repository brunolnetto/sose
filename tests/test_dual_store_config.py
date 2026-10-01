from pathlib import Path

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
