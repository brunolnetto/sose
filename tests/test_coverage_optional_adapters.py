from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from sose.persistence import ducklake as ducklake_module
from sose.persistence.ducklake import DuckLakePersistence


class FakeConnection:
    def __init__(self, *, row=None) -> None:
        self.row = row
        self.calls: list[tuple[str, object | None]] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self

    def fetchone(self):
        return self.row


def test_ducklake_constructor_configures_catalog_without_real_extension(
    tmp_path, monkeypatch
):
    connection = FakeConnection()
    monkeypatch.setattr(ducklake_module.duckdb, "connect", lambda target: connection)
    initialized = []
    refreshed = []
    monkeypatch.setattr(
        DuckLakePersistence,
        "_initialize_schema",
        lambda self: initialized.append(True),
    )
    monkeypatch.setattr(
        DuckLakePersistence,
        "_refresh_from_db",
        lambda self: refreshed.append(True),
    )

    catalog = tmp_path / "catalog" / "sose.ducklake"
    data_path = tmp_path / "lake-data"
    persistence = DuckLakePersistence(
        catalog,
        data_path=data_path,
        alias="quality_lake",
    )

    assert persistence.path == str(catalog)
    assert catalog.parent.is_dir()
    assert data_path.is_dir()
    assert initialized == [True]
    assert refreshed == [True]
    assert connection.calls == [
        ("INSTALL ducklake", None),
        ("LOAD ducklake", None),
        (
            "ATTACH ? AS quality_lake (TYPE DUCKLAKE, DATA_PATH ?)",
            [str(catalog), str(data_path)],
        ),
        ("USE quality_lake", None),
    ]


def _uninitialized_ducklake(connection):
    persistence = object.__new__(DuckLakePersistence)
    persistence._connection = connection
    return persistence


def test_ducklake_initialize_schema_bootstraps_missing_metadata():
    connection = FakeConnection(row=None)
    persistence = _uninitialized_ducklake(connection)

    persistence._initialize_schema()

    sql = "\n".join(call[0] for call in connection.calls)
    assert "CREATE TABLE IF NOT EXISTS sose_record_meta" in sql
    assert "CREATE TABLE IF NOT EXISTS sose_record" in sql
    assert "SELECT schema_version" in sql
    assert "INSERT INTO sose_record_meta VALUES (1, 1)" in sql


def test_ducklake_initialize_schema_accepts_current_version():
    connection = FakeConnection(row=(1,))
    persistence = _uninitialized_ducklake(connection)

    persistence._initialize_schema()

    assert not any(
        "INSERT INTO sose_record_meta" in sql
        for sql, _ in connection.calls
    )


def test_ducklake_initialize_schema_rejects_unknown_version():
    connection = FakeConnection(row=(2,))
    persistence = _uninitialized_ducklake(connection)

    with pytest.raises(RuntimeError, match="unsupported DuckLakePersistence schema version: 2"):
        persistence._initialize_schema()


def test_ducklake_apply_changes_covers_upsert_delete_and_count(monkeypatch):
    connection = FakeConnection()
    persistence = _uninitialized_ducklake(connection)
    changes = (
        SimpleNamespace(
            operation="delete",
            collection="events",
            key="old",
            position=None,
            payload=None,
        ),
        SimpleNamespace(
            operation="upsert",
            collection="events",
            key="new",
            position=4,
            payload='{"event":1}',
        ),
    )
    monkeypatch.setattr(
        ducklake_module,
        "changes_for_dirty_records",
        lambda before, after, dirty: changes,
    )

    count = persistence._apply_changes(object(), object(), {("events", "new")})

    assert count == 2
    deletes = [call for call in connection.calls if call[0].startswith("DELETE FROM")]
    inserts = [call for call in connection.calls if "INSERT INTO sose_record(" in call[0]]
    assert len(deletes) == 2
    assert inserts == [
        (
            """
                    INSERT INTO sose_record(collection, record_key, position, payload)
                    VALUES (?, ?, ?, ?)
                    """,
            ["events", "new", 4, '{"event":1}'],
        )
    ]


@pytest.mark.parametrize(
    "change",
    [
        SimpleNamespace(
            operation="upsert",
            collection="events",
            key="bad",
            position=None,
            payload="payload",
        ),
        SimpleNamespace(
            operation="upsert",
            collection="events",
            key="bad",
            position=0,
            payload=None,
        ),
    ],
)
def test_ducklake_apply_changes_rejects_incomplete_upsert(monkeypatch, change):
    persistence = _uninitialized_ducklake(FakeConnection())
    monkeypatch.setattr(
        ducklake_module,
        "changes_for_dirty_records",
        lambda before, after, dirty: (change,),
    )

    with pytest.raises(RuntimeError, match="upsert change requires position and payload"):
        persistence._apply_changes(object(), object(), set())


def test_ducklake_apply_changes_rejects_unknown_operation(monkeypatch):
    persistence = _uninitialized_ducklake(FakeConnection())
    monkeypatch.setattr(
        ducklake_module,
        "changes_for_dirty_records",
        lambda before, after, dirty: (
            SimpleNamespace(
                operation="mystery",
                collection="events",
                key="bad",
                position=None,
                payload=None,
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="unknown state record operation: mystery"):
        persistence._apply_changes(object(), object(), set())


def test_domain_catalog_module_constructs_catalog():
    module = importlib.import_module("sose.domain.catalog")
    module = importlib.reload(module)

    assert module.catalog is not None
    assert module.catalog.names() == ()
