from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from psycopg import sql

from sose.persistence import postgres as postgres_module
from sose.persistence import sqlite as sqlite_module
from sose.persistence.codec import dumps
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.memory import _State
from sose.persistence.postgres import PostgresPersistence
from sose.persistence.records import StateRecordChange
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


class _FetchOneResult:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _StaticConnection:
    def __init__(self, row) -> None:
        self._row = row

    def execute(self, *_args, **_kwargs):
        return _FetchOneResult(self._row)


def test_sqlite_refresh_guards_and_wrapper_methods():
    with SQLitePersistence(":memory:") as persistence:
        assert persistence.domain_delivery("missing") is None
        assert persistence.domain_deliveries() == ()
        assert persistence.committed_tick() == -1


def test_sqlite_refresh_rejects_schema_and_codec_mismatch():
    schema_probe = object.__new__(SQLitePersistence)
    schema_probe._connection = _StaticConnection(
        (
            sqlite_module.CURRENT_SCHEMA_VERSION + 1,
            sqlite_module.CURRENT_CODEC_VERSION,
            dumps(_State()),
        )
    )
    with pytest.raises(RuntimeError, match="schema was not migrated"):
        SQLitePersistence._refresh_from_db(schema_probe)

    codec_probe = object.__new__(SQLitePersistence)
    codec_probe._connection = _StaticConnection(
        (
            sqlite_module.CURRENT_SCHEMA_VERSION,
            sqlite_module.CURRENT_CODEC_VERSION + 1,
            dumps(_State()),
        )
    )
    with pytest.raises(RuntimeError, match="codec is newer"):
        SQLitePersistence._refresh_from_db(codec_probe)


def test_sqlite_refresh_rejects_payload_that_is_not_state(monkeypatch):
    probe = object.__new__(SQLitePersistence)
    probe._connection = _StaticConnection(
        (
            sqlite_module.CURRENT_SCHEMA_VERSION,
            sqlite_module.CURRENT_CODEC_VERSION,
            dumps(_State()),
        )
    )
    monkeypatch.setattr(sqlite_module, "loads", lambda _payload: {"not": "state"})

    with pytest.raises(TypeError, match="does not contain SOSE durable state"):
        SQLitePersistence._refresh_from_db(probe)


def test_sqlite_del_swallows_close_failures():
    probe = object.__new__(SQLitePersistence)
    probe.close = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    SQLitePersistence.__del__(probe)


def test_sqlite_transaction_accepts_precommitted_uow():
    persistence = SQLitePersistence(":memory:")
    with persistence.transaction() as uow:
        uow.commit()
    persistence.close()


def test_sqlite_close_tolerates_missing_connection_reference():
    probe = object.__new__(SQLitePersistence)
    probe._closed = False
    probe._connection = None
    SQLitePersistence.close(probe)
    assert probe._closed is True


def test_jsonl_journal_guard_paths_and_wrappers(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text("\n", encoding="utf-8")
    persistence = JSONLJournalPersistence(path)

    with persistence.transaction() as uow:
        uow.commit()

    persistence._fresh = persistence._refresh_from_journal
    assert persistence.domain_delivery("missing") is None
    assert persistence.domain_deliveries() == ()
    assert persistence.committed_tick() == -1

    with pytest.raises(RuntimeError, match="unknown JSONL journal operation"):
        JSONLJournalPersistence._apply_change(
            {},
            StateRecordChange(
                operation="unknown",
                collection="entities",
                key="k1",
                position=None,
                payload=None,
            ),
        )

    with pytest.raises(RuntimeError, match="upsert without payload"):
        JSONLJournalPersistence._apply_change(
            {},
            StateRecordChange(
                operation="upsert",
                collection="entities",
                key="k1",
                position=None,
                payload=None,
            ),
        )


def test_jsonl_journal_rejects_mid_file_corruption(tmp_path):
    path = tmp_path / "corrupt.jsonl"
    path.write_text(
        '{"schema_version":1\n'
        '{"schema_version":1,"transaction":1,"changes":[]}\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="corrupt JSONL journal at line 1"):
        JSONLJournalPersistence(path)


def test_jsonl_journal_rejects_unknown_schema_version(tmp_path):
    path = tmp_path / "schema.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": 99,
                "transaction": 1,
                "changes": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unsupported JSONLJournalPersistence"):
        JSONLJournalPersistence(path)


def test_duckdb_context_wrappers_and_closed_uow_branch():
    with DuckDBPersistence(":memory:") as persistence:
        with persistence.transaction() as uow:
            uow.commit()

        assert persistence.domain_delivery("missing") is None
        assert persistence.domain_deliveries() == ()
        assert persistence.committed_tick() == -1


def test_duckdb_rejects_unsupported_schema_version(tmp_path):
    path = tmp_path / "unsupported.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        """
        CREATE TABLE sose_record_meta (
            singleton INTEGER PRIMARY KEY,
            schema_version INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE sose_record (
            collection VARCHAR NOT NULL,
            record_key VARCHAR NOT NULL,
            position BIGINT NOT NULL,
            payload VARCHAR NOT NULL,
            PRIMARY KEY (collection, record_key)
        )
        """
    )
    connection.execute("INSERT INTO sose_record_meta VALUES (1, 99)")
    connection.close()

    with pytest.raises(RuntimeError, match="unsupported DuckDBPersistence schema"):
        DuckDBPersistence(path)


def test_sqlite_incremental_guard_paths(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="namespace must be"):
        SQLiteIncrementalPersistence(tmp_path / "bad.sqlite3", namespace="BAD-NAME")

    memory_probe = object.__new__(SQLiteIncrementalPersistence)
    memory_probe.path = ":memory:"
    monkeypatch.setattr(
        Path,
        "mkdir",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mkdir called")),
    )
    SQLiteIncrementalPersistence._ensure_parent_path(memory_probe)

    retry_probe = object.__new__(SQLiteIncrementalPersistence)

    class _LockedThenWal:
        def __init__(self):
            self.calls = 0

        def execute(self, _statement):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return _FetchOneResult(("wal",))

    retry_probe._connection = _LockedThenWal()
    monotonic_values = iter([0.0, 0.0])
    sleep_calls: list[float] = []
    from sose.persistence import sqlite_incremental as incremental_module

    monkeypatch.setattr(incremental_module, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(incremental_module, "sleep", lambda value: sleep_calls.append(value))
    SQLiteIncrementalPersistence._ensure_wal(retry_probe)
    assert sleep_calls == [0.01]

    bad_mode_probe = object.__new__(SQLiteIncrementalPersistence)
    bad_mode_probe._connection = SimpleNamespace(
        execute=lambda _statement: _FetchOneResult(("delete",))
    )
    monkeypatch.setattr(incremental_module, "monotonic", lambda: 0.0)
    with pytest.raises(RuntimeError, match="requires WAL journal mode"):
        SQLiteIncrementalPersistence._ensure_wal(bad_mode_probe)

    timeout_probe = object.__new__(SQLiteIncrementalPersistence)
    timeout_probe._connection = SimpleNamespace(
        execute=lambda _statement: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        )
    )
    timeout_values = iter([0.0, 31.0])
    monkeypatch.setattr(incremental_module, "monotonic", lambda: next(timeout_values))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        SQLiteIncrementalPersistence._ensure_wal(timeout_probe)


def test_sqlite_incremental_bootstrap_rollback_and_metadata_guards(monkeypatch):
    class _BootstrapConnection:
        def __init__(self):
            self.rolled_back = False

        def execute(self, _statement):
            return None

        def commit(self):
            return None

        def rollback(self):
            self.rolled_back = True

    probe = object.__new__(SQLiteIncrementalPersistence)
    probe._connection = _BootstrapConnection()

    monkeypatch.setattr(
        SQLiteIncrementalPersistence,
        "_create_tables",
        lambda _self: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        SQLiteIncrementalPersistence._bootstrap_schema(probe)
    assert probe._connection.rolled_back is True

    migration_probe = object.__new__(SQLiteIncrementalPersistence)
    migration_probe._meta_table = '"meta"'
    statements: list[str] = []
    migration_probe._connection = SimpleNamespace(
        execute=lambda statement, *_args, **_kwargs: statements.append(str(statement))
    )
    SQLiteIncrementalPersistence._migrate_schema(
        migration_probe,
        2,
        {"owner_id", "owner_epoch", "revision"},
    )
    SQLiteIncrementalPersistence._migrate_schema(
        migration_probe,
        1,
        {"revision", "owner_id", "owner_epoch"},
    )
    assert any("SET schema_version = 3" in statement for statement in statements)
    assert not any("ADD COLUMN owner_id" in statement for statement in statements)
    assert not any("ADD COLUMN owner_epoch" in statement for statement in statements)

    with pytest.raises(RuntimeError, match="unsupported SQLiteIncrementalPersistence"):
        SQLiteIncrementalPersistence._migrate_schema(
            migration_probe,
            99,
            set(),
        )

    revision_probe = object.__new__(SQLiteIncrementalPersistence)
    revision_probe._connection = _StaticConnection(None)
    revision_probe._meta_table = '"meta"'
    assert SQLiteIncrementalPersistence._database_revision(revision_probe) == 0

    writer_probe = object.__new__(SQLiteIncrementalPersistence)
    writer_probe._connection = _StaticConnection(None)
    writer_probe._meta_table = '"meta"'
    with pytest.raises(RuntimeError, match="writer metadata disappeared"):
        SQLiteIncrementalPersistence.writer_epoch(writer_probe)

    with pytest.raises(ValueError, match="owner_id cannot be empty"):
        SQLiteIncrementalPersistence.claim_writer(writer_probe, "", expected_epoch=0)
    with pytest.raises(ValueError, match="expected_epoch must be >= 0"):
        SQLiteIncrementalPersistence.claim_writer(
            writer_probe,
            "owner",
            expected_epoch=-1,
        )


def test_sqlite_incremental_close_and_context_and_destructor_paths(tmp_path):
    probe = object.__new__(SQLiteIncrementalPersistence)
    probe._closed = False
    probe._connection = None
    SQLiteIncrementalPersistence.close(probe)
    assert probe._closed is True

    with SQLiteIncrementalPersistence(tmp_path / "lifecycle.sqlite3") as persistence:
        assert persistence is persistence
        with persistence.transaction() as uow:
            uow.commit()

    destructor_probe = object.__new__(SQLiteIncrementalPersistence)
    destructor_probe.close = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    SQLiteIncrementalPersistence.__del__(destructor_probe)


class _FakePgResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _FakePgTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakePgConnection:
    def __init__(self, *, schema_version: int = 2, revision_row=(0,), epoch_row=(0,)):
        self.schema_version = schema_version
        self.revision_row = revision_row
        self.epoch_row = epoch_row

    def transaction(self):
        return _FakePgTransaction()

    def execute(self, statement, *_args, **_kwargs):
        text = str(statement)
        if "SELECT schema_version" in text:
            return _FakePgResult((self.schema_version,))
        if "SELECT revision" in text:
            return _FakePgResult(self.revision_row)
        if "SELECT owner_epoch" in text:
            return _FakePgResult(self.epoch_row)
        if "SELECT collection, record_key, position, payload" in text:
            return _FakePgResult([])
        return _FakePgResult(None)

    def close(self):
        return None


def test_postgres_validation_and_guard_paths(monkeypatch):
    with pytest.raises(ValueError, match="dsn cannot be empty"):
        PostgresPersistence("", namespace="sose")
    with pytest.raises(ValueError, match="SQL-safe identifier"):
        PostgresPersistence("postgresql://example", namespace="bad-name")

    monkeypatch.setattr(
        postgres_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: _FakePgConnection(schema_version=99),
    )
    with pytest.raises(RuntimeError, match="unsupported PostgresPersistence schema"):
        PostgresPersistence("postgresql://example", namespace="sose")

    probe = object.__new__(PostgresPersistence)
    closed: list[str] = []
    probe.close = lambda: closed.append("closed")
    assert PostgresPersistence.__enter__(probe) is probe
    PostgresPersistence.__exit__(probe, None, None, None)
    assert closed == ["closed"]

    metadata_probe = object.__new__(PostgresPersistence)
    metadata_probe._connection = _FakePgConnection(revision_row=None, epoch_row=None)
    metadata_probe._meta_table = sql.Identifier("meta")
    with pytest.raises(RuntimeError, match="metadata row is missing"):
        PostgresPersistence._database_revision(metadata_probe)
    with pytest.raises(RuntimeError, match="writer metadata disappeared"):
        PostgresPersistence.writer_epoch(metadata_probe)
    with pytest.raises(ValueError, match="owner_id cannot be empty"):
        PostgresPersistence.claim_writer(metadata_probe, "", expected_epoch=0)
    with pytest.raises(ValueError, match="expected_epoch must be >= 0"):
        PostgresPersistence.claim_writer(
            metadata_probe,
            "owner",
            expected_epoch=-1,
        )


def test_postgres_transaction_accepts_precommitted_uow_branch():
    probe = object.__new__(PostgresPersistence)
    probe._connection = SimpleNamespace(transaction=lambda: _FakePgTransaction())
    probe._revision = 7
    probe._state = _State()
    probe._lock_and_validate_epoch = lambda _owner_epoch: 0
    closed_uow = SimpleNamespace(_closed=True, dirty_records=())
    probe._begin_transaction_uow = lambda: (_State(), closed_uow)
    probe._apply_changes = lambda _before, _after, _dirty: 0
    probe._increment_revision = lambda: 99

    with PostgresPersistence.transaction(probe) as uow:
        assert uow is closed_uow

    assert probe._revision == 7
