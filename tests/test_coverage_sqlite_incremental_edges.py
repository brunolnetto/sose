from __future__ import annotations

import sqlite3

import pytest

import sose.persistence.sqlite_incremental as sqlite_incremental
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def _create_legacy_database(
    path,
    *,
    version: int,
    revision: bool = False,
    owner_id: bool = False,
    owner_epoch: bool = False,
) -> None:
    columns = [
        "singleton INTEGER PRIMARY KEY CHECK (singleton = 1)",
        "schema_version INTEGER NOT NULL",
    ]
    values = ["1", str(version)]
    if revision:
        columns.append("revision INTEGER NOT NULL DEFAULT 0")
        values.append("0")
    if owner_id:
        columns.append("owner_id TEXT")
        values.append("NULL")
    if owner_epoch:
        columns.append("owner_epoch INTEGER NOT NULL DEFAULT 0")
        values.append("0")

    connection = sqlite3.connect(path)
    connection.execute(
        f"CREATE TABLE sose_record_meta ({', '.join(columns)})"
    )
    connection.execute(
        """
        CREATE TABLE sose_record (
            collection TEXT NOT NULL,
            record_key TEXT NOT NULL,
            position INTEGER NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (collection, record_key)
        )
        """
    )
    connection.execute(
        f"INSERT INTO sose_record_meta VALUES ({', '.join(values)})"
    )
    connection.commit()
    connection.close()


@pytest.mark.parametrize(
    ("version", "revision", "owner_id", "owner_epoch"),
    [
        (1, True, False, False),
        (2, True, True, False),
        (2, True, True, True),
    ],
)
def test_legacy_schema_migration_accepts_columns_already_present(
    tmp_path,
    version,
    revision,
    owner_id,
    owner_epoch,
):
    path = tmp_path / f"legacy-{version}-{owner_id}-{owner_epoch}.sqlite3"
    _create_legacy_database(
        path,
        version=version,
        revision=revision,
        owner_id=owner_id,
        owner_epoch=owner_epoch,
    )

    persistence = SQLiteIncrementalPersistence(path)
    persistence.close()

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT schema_version, revision, owner_id, owner_epoch
            FROM sose_record_meta
            WHERE singleton = 1
            """
        ).fetchone()
    assert row == (3, 0, None, 0)


def test_future_schema_is_rejected_and_constructor_rolls_back(tmp_path):
    path = tmp_path / "future.sqlite3"
    _create_legacy_database(
        path,
        version=99,
        revision=True,
        owner_id=True,
        owner_epoch=True,
    )

    with pytest.raises(RuntimeError, match="unsupported .* schema version"):
        SQLiteIncrementalPersistence(path)

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT schema_version FROM sose_record_meta WHERE singleton = 1"
        ).fetchone()
    assert row == (99,)


def test_memory_database_reaches_non_wal_rejection():
    with pytest.raises(RuntimeError, match="requires WAL journal mode"):
        SQLiteIncrementalPersistence(":memory:")


class _WalCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _WalConnection:
    def __init__(self, outcomes):
        self._outcomes = iter(outcomes)

    def execute(self, sql):
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return _WalCursor(outcome)


def _wal_probe(outcomes):
    persistence = object.__new__(SQLiteIncrementalPersistence)
    persistence._connection = _WalConnection(outcomes)
    return persistence


def test_wal_retry_recovers_from_transient_locked_database(monkeypatch):
    probe = _wal_probe(
        [
            sqlite3.OperationalError("database is locked"),
            ("wal",),
        ]
    )
    monkeypatch.setattr(sqlite_incremental, "sleep", lambda _: None)

    probe._ensure_wal()


def test_wal_retry_does_not_hide_non_lock_operational_error():
    probe = _wal_probe([sqlite3.OperationalError("disk I/O error")])

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        probe._ensure_wal()


def test_wal_retry_stops_after_deadline(monkeypatch):
    probe = _wal_probe([sqlite3.OperationalError("database is locked")])
    values = iter([0.0, 31.0])
    monkeypatch.setattr(sqlite_incremental, "monotonic", lambda: next(values))

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        probe._ensure_wal()


def test_wal_requires_an_explicit_wal_result():
    probe = _wal_probe([None])

    with pytest.raises(RuntimeError, match="requires WAL journal mode"):
        probe._ensure_wal()


def test_context_manager_closes_connection(tmp_path):
    path = tmp_path / "context.sqlite3"
    with SQLiteIncrementalPersistence(path) as persistence:
        connection = persistence._connection
        assert persistence.committed_tick() == -1

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_missing_metadata_revision_returns_zero_and_writer_epoch_rejects(tmp_path):
    persistence = SQLiteIncrementalPersistence(tmp_path / "missing-meta.sqlite3")
    persistence._connection.execute(
        "DELETE FROM sose_record_meta WHERE singleton = 1"
    )
    persistence._connection.commit()

    assert persistence._database_revision() == 0
    with pytest.raises(RuntimeError, match="writer metadata disappeared"):
        persistence.writer_epoch()
    persistence.close()


@pytest.mark.parametrize(
    ("owner_id", "expected_epoch", "message"),
    [
        ("", 0, "owner_id cannot be empty"),
        ("worker", -1, "expected_epoch must be >= 0"),
    ],
)
def test_claim_writer_validates_inputs(
    tmp_path,
    owner_id,
    expected_epoch,
    message,
):
    persistence = SQLiteIncrementalPersistence(tmp_path / "writer.sqlite3")
    try:
        with pytest.raises(ValueError, match=message):
            persistence.claim_writer(owner_id, expected_epoch=expected_epoch)
    finally:
        persistence.close()


def test_transaction_respects_explicit_uow_commit(tmp_path):
    persistence = SQLiteIncrementalPersistence(tmp_path / "explicit.sqlite3")
    try:
        with persistence.transaction() as uow:
            uow.set_committed_tick(7)
            uow.commit()

        assert persistence.committed_tick() == 7
    finally:
        persistence.close()
