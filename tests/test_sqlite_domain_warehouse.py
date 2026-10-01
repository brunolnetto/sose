from concurrent.futures import ThreadPoolExecutor

import pytest

from sose.domain.entity import Entity
from sose.domain.sqlite import SQLiteDomainWarehouse
from sose.domain.warehouse import DomainApplyResult, DomainMutation


def test_sqlite_domain_warehouse_reopens_and_rejects_stale_version(tmp_path):
    path = tmp_path / "domain.db"
    first = SQLiteDomainWarehouse(path)
    v2 = Entity(id="1", entity_type="order", state="running", version=2)
    assert first.apply(DomainMutation("v2", v2)) is DomainApplyResult.APPLIED
    first.close()

    reopened = SQLiteDomainWarehouse(path)
    assert reopened.entity("order", "1") == v2
    v1 = Entity(id="1", entity_type="order", state="queued", version=1)
    assert reopened.apply(DomainMutation("v1", v1)) is DomainApplyResult.SUPERSEDED
    assert reopened.entity("order", "1") == v2
    reopened.close()


def test_sqlite_domain_warehouse_durable_replay_and_conflicts(tmp_path):
    path = tmp_path / "domain.db"
    warehouse = SQLiteDomainWarehouse(path)
    v2 = Entity(id="1", entity_type="order", state="running", version=2)
    mutation = DomainMutation("v2", v2)
    assert warehouse.apply(mutation) is DomainApplyResult.APPLIED
    warehouse.close()

    reopened = SQLiteDomainWarehouse(path)
    assert reopened.apply(mutation) is DomainApplyResult.REPLAYED
    with pytest.raises(ValueError, match="mutation identity conflict"):
        reopened.apply(DomainMutation("v2", Entity(id="1", entity_type="order", state="closed", version=3)))
    with pytest.raises(ValueError, match="conflicting domain entity version"):
        reopened.apply(DomainMutation("other-v2", Entity(id="1", entity_type="order", state="closed", version=2)))
    reopened.close()


def test_sqlite_domain_warehouse_concurrent_constructors(tmp_path):
    path = tmp_path / "domain.db"

    def open_and_close(_):
        warehouse = SQLiteDomainWarehouse(path)
        warehouse.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(open_and_close, range(32)))
