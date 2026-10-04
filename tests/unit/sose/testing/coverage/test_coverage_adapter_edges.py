from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sose.domain.catalog import catalog
from sose.domain.config import DomainCatalog
from sose.persistence import ducklake as ducklake_module
from sose.persistence.ducklake import DuckLakePersistence


class FakeConnection:
    def __init__(self, rows=()):
        self.rows = iter(rows)
        self.executed: list[tuple[str, object | None]] = []

    def execute(self, statement, params=None):
        self.executed.append((" ".join(str(statement).split()), params))
        return self

    def fetchone(self):
        return next(self.rows, None)


def _uninitialized_ducklake(connection: FakeConnection) -> DuckLakePersistence:
    persistence = DuckLakePersistence.__new__(DuckLakePersistence)
    persistence._connection = connection
    return persistence


def test_default_domain_catalog_module_is_importable_and_empty():
    assert isinstance(catalog, DomainCatalog)
    assert catalog.names() == ()
    assert catalog.definitions() == ()


def test_ducklake_initialization_builds_catalog_and_data_paths(tmp_path, monkeypatch):
    connection = FakeConnection()
    lifecycle: list[str] = []

    monkeypatch.setattr(
        ducklake_module.duckdb,
        "connect",
        lambda path: connection,
    )
    monkeypatch.setattr(
        DuckLakePersistence,
        "_initialize_schema",
        lambda self: lifecycle.append("schema"),
    )
    monkeypatch.setattr(
        DuckLakePersistence,
        "_refresh_from_db",
        lambda self: lifecycle.append("refresh"),
    )

    catalog_path = tmp_path / "catalog" / "sose.ducklake"
    data_path = tmp_path / "lake-data"
    persistence = DuckLakePersistence(
        catalog_path,
        data_path=data_path,
        alias="quality_lake",
    )

    assert persistence.path == str(catalog_path)
    assert catalog_path.parent.is_dir()
    assert data_path.is_dir()
    assert lifecycle == ["schema", "refresh"]
    assert connection.executed == [
        ("INSTALL ducklake", None),
        ("LOAD ducklake", None),
        (
            "ATTACH ? AS quality_lake (TYPE DUCKLAKE, DATA_PATH ?)",
            [str(catalog_path), str(data_path)],
        ),
        ("USE quality_lake", None),
    ]


def test_ducklake_schema_bootstraps_and_accepts_current_version():
    bootstrap = FakeConnection(rows=[None])
    _uninitialized_ducklake(bootstrap)._initialize_schema()
    assert any(
        statement == "INSERT INTO sose_record_meta VALUES (1, 1)"
        for statement, _ in bootstrap.executed
    )

    current = FakeConnection(rows=[(1,)])
    _uninitialized_ducklake(current)._initialize_schema()
    assert not any(
        statement.startswith("INSERT INTO sose_record_meta")
        for statement, _ in current.executed
    )


def test_ducklake_schema_rejects_unknown_version():
    connection = FakeConnection(rows=[(2,)])

    with pytest.raises(
        RuntimeError,
        match="unsupported DuckLakePersistence schema version: 2",
    ):
        _uninitialized_ducklake(connection)._initialize_schema()


def test_ducklake_applies_delete_and_upsert_changes(monkeypatch):
    connection = FakeConnection()
    persistence = _uninitialized_ducklake(connection)
    changes = (
        SimpleNamespace(
            operation="delete",
            collection="commands",
            key="old",
            position=None,
            payload=None,
        ),
        SimpleNamespace(
            operation="upsert",
            collection="commands",
            key="new",
            position=4,
            payload='{"value":1}',
        ),
    )
    monkeypatch.setattr(
        ducklake_module,
        "changes_for_dirty_records",
        lambda before, after, dirty: changes,
    )

    assert persistence._apply_changes(object(), object(), {("commands", "new")}) == 2
    assert connection.executed == [
        (
            "DELETE FROM sose_record WHERE collection = ? AND record_key = ?",
            ["commands", "old"],
        ),
        (
            "DELETE FROM sose_record WHERE collection = ? AND record_key = ?",
            ["commands", "new"],
        ),
        (
            "INSERT INTO sose_record(collection, record_key, position, payload) VALUES (?, ?, ?, ?)",
            ["commands", "new", 4, '{"value":1}'],
        ),
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            SimpleNamespace(
                operation="upsert",
                collection="commands",
                key="broken",
                position=None,
                payload="payload",
            ),
            "upsert change requires position and payload",
        ),
        (
            SimpleNamespace(
                operation="mystery",
                collection="commands",
                key="broken",
                position=0,
                payload="payload",
            ),
            "unknown state record operation: mystery",
        ),
    ],
)
def test_ducklake_rejects_invalid_internal_changes(monkeypatch, change, message):
    persistence = _uninitialized_ducklake(FakeConnection())
    monkeypatch.setattr(
        ducklake_module,
        "changes_for_dirty_records",
        lambda before, after, dirty: (change,),
    )

    with pytest.raises(RuntimeError, match=message):
        persistence._apply_changes(object(), object(), ())
