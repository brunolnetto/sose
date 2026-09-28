from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Callable


CURRENT_SCHEMA_VERSION = 2
CURRENT_CODEC_VERSION = 1


@dataclass(frozen=True, slots=True)
class SQLiteSchemaInfo:
    schema_version: int
    codec_version: int


Migration = Callable[[sqlite3.Connection], None]


def ensure_schema(connection: sqlite3.Connection) -> SQLiteSchemaInfo:
    """Create or migrate the SOSE SQLite schema to the current version."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sose_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL,
            codec_version INTEGER NOT NULL DEFAULT 1,
            payload TEXT NOT NULL
        )
        """
    )

    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(sose_state)").fetchall()
    }
    row = connection.execute(
        "SELECT schema_version FROM sose_state WHERE singleton = 1"
    ).fetchone()

    if row is None:
        if "codec_version" not in columns:
            connection.execute(
                "ALTER TABLE sose_state "
                "ADD COLUMN codec_version INTEGER NOT NULL DEFAULT 1"
            )
        return SQLiteSchemaInfo(
            schema_version=CURRENT_SCHEMA_VERSION,
            codec_version=CURRENT_CODEC_VERSION,
        )

    version = int(row[0])
    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            "SQLitePersistence database schema is newer than this runtime: "
            f"database={version}, runtime={CURRENT_SCHEMA_VERSION}"
        )

    while version < CURRENT_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise RuntimeError(
                "no SQLitePersistence migration path from schema "
                f"{version} to {CURRENT_SCHEMA_VERSION}"
            )
        migration(connection)
        version += 1

    info = read_schema_info(connection)
    if info.codec_version > CURRENT_CODEC_VERSION:
        raise RuntimeError(
            "SQLitePersistence codec is newer than this runtime: "
            f"database={info.codec_version}, runtime={CURRENT_CODEC_VERSION}"
        )
    return info


def read_schema_info(connection: sqlite3.Connection) -> SQLiteSchemaInfo:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(sose_state)").fetchall()
    }
    row = connection.execute(
        "SELECT schema_version"
        + (", codec_version" if "codec_version" in columns else "")
        + " FROM sose_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return SQLiteSchemaInfo(
            schema_version=CURRENT_SCHEMA_VERSION,
            codec_version=CURRENT_CODEC_VERSION,
        )
    return SQLiteSchemaInfo(
        schema_version=int(row[0]),
        codec_version=int(row[1]) if len(row) > 1 else 1,
    )


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(sose_state)").fetchall()
    }
    if "codec_version" not in columns:
        connection.execute(
            "ALTER TABLE sose_state "
            "ADD COLUMN codec_version INTEGER NOT NULL DEFAULT 1"
        )
    connection.execute(
        """
        UPDATE sose_state
        SET schema_version = 2,
            codec_version = 1
        WHERE singleton = 1
        """
    )


MIGRATIONS: dict[int, Migration] = {
    1: _migrate_v1_to_v2,
}
