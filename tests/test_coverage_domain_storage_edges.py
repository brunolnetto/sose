from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from sose.core.identity import deterministic_id
from sose.domain.delivery import DomainDelivery
from sose.domain.entity import Entity
from sose.domain.sqlite import SQLiteDomainWarehouse
from sose.domain import sqlite as sqlite_module
from sose.domain.storage import DomainPersistence
from sose.domain.warehouse import (
    DomainApplyResult,
    DomainMutation,
    MemoryDomainWarehouse,
)
from sose.persistence.memory import MemoryPersistence


class _WalConnection:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def execute(self, _sql):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(fetchone=lambda: outcome)


def _warehouse_with_connection(connection):
    warehouse = object.__new__(SQLiteDomainWarehouse)
    warehouse._connection = connection
    return warehouse


@pytest.mark.parametrize("row", [None, ("delete",)])
def test_sqlite_domain_warehouse_requires_wal_mode(row):
    warehouse = _warehouse_with_connection(_WalConnection([row]))
    with pytest.raises(RuntimeError, match="requires WAL journal mode"):
        warehouse._ensure_wal()


def test_sqlite_domain_warehouse_retries_locked_wal_initialization(monkeypatch):
    connection = _WalConnection(
        [
            sqlite3.OperationalError("database is locked"),
            ("wal",),
        ]
    )
    warehouse = _warehouse_with_connection(connection)
    monkeypatch.setattr(sqlite_module, "sleep", lambda _seconds: None)

    warehouse._ensure_wal()

    assert connection.calls == 2


def test_sqlite_domain_warehouse_propagates_non_lock_operational_errors():
    warehouse = _warehouse_with_connection(
        _WalConnection([sqlite3.OperationalError("disk I/O error")])
    )
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        warehouse._ensure_wal()


def test_sqlite_domain_warehouse_stops_retrying_after_deadline(monkeypatch):
    warehouse = _warehouse_with_connection(
        _WalConnection([sqlite3.OperationalError("database is locked")])
    )
    times = iter([0.0, 31.0])
    monkeypatch.setattr(sqlite_module, "monotonic", lambda: next(times))
    monkeypatch.setattr(sqlite_module, "sleep", lambda _seconds: None)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        warehouse._ensure_wal()


def test_sqlite_domain_warehouse_reads_filters_and_same_version_replay():
    warehouse = SQLiteDomainWarehouse(":memory:")
    try:
        first = Entity(
            id="1",
            entity_type="order",
            state="running",
            version=2,
        )
        second = Entity(
            id="2",
            entity_type="invoice",
            state="open",
            version=1,
        )
        assert warehouse.entity("order", "missing") is None
        assert warehouse.entities() == ()

        assert (
            warehouse.apply(DomainMutation("order-v2", first))
            is DomainApplyResult.APPLIED
        )
        assert (
            warehouse.apply(DomainMutation("invoice-v1", second))
            is DomainApplyResult.APPLIED
        )

        assert warehouse.entities("order") == (first,)
        assert warehouse.entities("missing") == ()
        assert warehouse.entities() == (second, first)

        # New mutation identity, same exact entity revision: semantic replay.
        assert (
            warehouse.apply(DomainMutation("order-v2-alias", first))
            is DomainApplyResult.REPLAYED
        )
    finally:
        warehouse.close()


def _domain_persistence():
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    return engine, warehouse, DomainPersistence(engine, warehouse)


def test_domain_uow_rejects_stale_write_against_warehouse():
    engine, warehouse, persistence = _domain_persistence()
    current = Entity(id="1", entity_type="order", state="running", version=2)
    warehouse.apply(DomainMutation("warehouse-v2", current))

    stale = Entity(id="1", entity_type="order", state="queued", version=1)
    with pytest.raises(ValueError, match="stale domain entity write"):
        with persistence.transaction() as uow:
            uow.save_entity(stale)

    assert engine.domain_deliveries() == ()


def test_domain_uow_bumps_revision_for_direct_attribute_change():
    engine, warehouse, persistence = _domain_persistence()
    current = Entity(
        id="1",
        entity_type="order",
        state="running",
        attributes={"priority": "normal"},
        version=2,
    )
    warehouse.apply(DomainMutation("warehouse-v2", current))

    changed = Entity(
        id="1",
        entity_type="order",
        state="running",
        attributes={"priority": "high"},
        version=2,
    )
    with persistence.transaction() as uow:
        uow.save_entity(changed)
        saved = uow.get_entity("order", "1")
        assert saved is not None
        assert saved.version == 3
        assert saved.attributes["priority"] == "high"

    deliveries = engine.domain_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].mutation.entity.version == 3


def test_domain_uow_reuses_existing_identical_delivery():
    engine, warehouse, persistence = _domain_persistence()
    entity = Entity(id="1", entity_type="order", state="queued", version=1)

    with persistence.transaction() as uow:
        uow.save_entity(entity)
    first = engine.domain_deliveries()

    with persistence.transaction() as uow:
        uow.save_entity(entity)

    assert engine.domain_deliveries() == first


def test_domain_uow_rejects_conflicting_existing_mutation_identity():
    engine, warehouse, persistence = _domain_persistence()
    desired = Entity(id="1", entity_type="order", state="queued", version=1)
    mutation_id = deterministic_id(
        "domain-entity-version",
        desired.entity_type,
        desired.id,
        desired.version,
    )
    conflict = DomainMutation(
        mutation_id,
        Entity(id="1", entity_type="order", state="different", version=1),
    )
    with engine.transaction() as uow:
        uow.save_domain_delivery(DomainDelivery(conflict))

    with pytest.raises(ValueError, match="domain mutation identity conflict"):
        with persistence.transaction() as uow:
            uow.save_entity(desired)


def test_domain_persistence_entities_prefers_newer_pending_delivery():
    engine, warehouse, persistence = _domain_persistence()
    v1 = Entity(id="1", entity_type="order", state="queued", version=1)
    warehouse.apply(DomainMutation("warehouse-v1", v1))

    v2 = Entity(id="1", entity_type="order", state="running", version=2)
    with persistence.transaction() as uow:
        uow.save_entity(v2)

    assert persistence.entity("order", "1") == v2
    assert persistence.entities() == (v2,)


def test_domain_persistence_entities_includes_pending_entity_not_in_warehouse():
    _, _, persistence = _domain_persistence()
    pending = Entity(id="1", entity_type="order", state="queued", version=1)

    with persistence.transaction() as uow:
        uow.save_entity(pending)

    assert persistence.entities() == (pending,)


def test_domain_persistence_entities_rejects_equal_version_conflict():
    engine, warehouse, persistence = _domain_persistence()
    warehouse_entity = Entity(
        id="1",
        entity_type="order",
        state="queued",
        version=1,
    )
    warehouse.apply(DomainMutation("warehouse-v1", warehouse_entity))

    conflicting = Entity(
        id="1",
        entity_type="order",
        state="running",
        version=1,
    )
    mutation = DomainMutation("conflicting-pending", conflicting)
    with engine.transaction() as uow:
        uow.save_domain_delivery(DomainDelivery(mutation))

    with pytest.raises(RuntimeError, match="conflicting domain entity version"):
        persistence.entities()


def test_domain_persistence_forwards_engine_attributes():
    engine, _, persistence = _domain_persistence()
    assert persistence.committed_tick() == engine.committed_tick()
