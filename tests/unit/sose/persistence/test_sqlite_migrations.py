import sqlite3

import pytest

from sose.core.runtime import ResourceDefinition
from sose.persistence.codec import dumps
from sose.persistence.memory import _State
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_migrations import (
    CURRENT_CODEC_VERSION,
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    ensure_schema,
    read_schema_info,
)


def _legacy_v1_database(path, *, state=None):
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE sose_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    if state is not None:
        connection.execute(
            "INSERT INTO sose_state(singleton, schema_version, payload) "
            "VALUES (1, 1, ?)",
            (dumps(state),),
        )
    connection.commit()
    connection.close()


def test_populated_v1_database_migrates_and_preserves_durable_state(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    state = _State(
        resource_definitions={
            "bay": ResourceDefinition("bay", capacity=2),
        },
        committed_tick=7,
    )
    _legacy_v1_database(path, state=state)

    with SQLitePersistence(path) as persistence:
        assert persistence.schema_info().schema_version == CURRENT_SCHEMA_VERSION
        assert persistence.schema_info().codec_version == CURRENT_CODEC_VERSION
        assert persistence.resource_definitions() == (
            ResourceDefinition("bay", capacity=2),
        )
        assert persistence.committed_tick() == 7

        columns = {
            row[1]
            for row in persistence._connection.execute(
                "PRAGMA table_info(sose_state)"
            ).fetchall()
        }
        assert "codec_version" in columns


def test_empty_v1_schema_is_structurally_upgraded_before_first_write(tmp_path):
    path = tmp_path / "empty-legacy.sqlite3"
    _legacy_v1_database(path)

    with SQLitePersistence(path) as persistence:
        with persistence.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("worker", capacity=1))

        assert persistence.schema_info().schema_version == CURRENT_SCHEMA_VERSION
        assert persistence.resource_definitions() == (
            ResourceDefinition("worker", capacity=1),
        )


def test_migrated_database_reopens_without_reapplying_migration(tmp_path):
    path = tmp_path / "reopen.sqlite3"
    _legacy_v1_database(
        path,
        state=_State(committed_tick=3),
    )

    with SQLitePersistence(path):
        pass
    with SQLitePersistence(path) as second:
        assert second.schema_info().schema_version == CURRENT_SCHEMA_VERSION
        assert second.committed_tick() == 3


def test_future_schema_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE sose_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            codec_version INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO sose_state(singleton, schema_version, codec_version, payload) "
        "VALUES (1, ?, 1, ?)",
        (CURRENT_SCHEMA_VERSION + 1, dumps(_State(committed_tick=11))),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="newer than this runtime"):
        SQLitePersistence(path)

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT schema_version, codec_version FROM sose_state WHERE singleton = 1"
    ).fetchone()
    connection.close()
    assert row == (CURRENT_SCHEMA_VERSION + 1, 1)


def test_future_codec_is_rejected_without_interpreting_payload(tmp_path):
    path = tmp_path / "future-codec.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE sose_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            codec_version INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO sose_state(singleton, schema_version, codec_version, payload) "
        "VALUES (1, ?, ?, ?)",
        (
            CURRENT_SCHEMA_VERSION,
            CURRENT_CODEC_VERSION + 1,
            dumps(_State()),
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="codec is newer"):
        SQLitePersistence(path)


def test_missing_migration_path_is_rejected(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE sose_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            codec_version INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO sose_state(singleton, schema_version, codec_version, payload) "
        "VALUES (1, 1, 1, ?)",
        (dumps(_State()),),
    )
    monkeypatch.setitem(MIGRATIONS, 1, None)
    with pytest.raises(RuntimeError, match="no SQLitePersistence migration path"):
        ensure_schema(connection)
    connection.close()


def test_read_schema_info_defaults_when_state_row_is_absent():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE sose_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            codec_version INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )

    info = read_schema_info(connection)

    assert info.schema_version == CURRENT_SCHEMA_VERSION
    assert info.codec_version == CURRENT_CODEC_VERSION
    connection.close()


def test_migration_v1_with_existing_codec_column_skips_alter_table(tmp_path):
    path = tmp_path / "legacy-with-codec.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE sose_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            codec_version INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO sose_state(singleton, schema_version, codec_version, payload) "
        "VALUES (1, 1, 1, ?)",
        (dumps(_State(committed_tick=5)),),
    )
    connection.commit()

    info = ensure_schema(connection)
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(sose_state)").fetchall()
    }
    row = connection.execute(
        "SELECT schema_version, codec_version FROM sose_state WHERE singleton = 1"
    ).fetchone()
    connection.close()

    assert info.schema_version == CURRENT_SCHEMA_VERSION
    assert "codec_version" in columns
    assert row == (CURRENT_SCHEMA_VERSION, CURRENT_CODEC_VERSION)
