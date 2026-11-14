"""Schema bootstrap and migration steps of the incremental SQLite adapter.

Split from ``sqlite_incremental.py`` (``SQLiteIncrementalPersistence``).
"""

from __future__ import annotations

import sqlite3

_SCHEMA_VERSION = 3


class SQLiteIncrementalSchemaMixin:
    """Creates and migrates the namespaced metadata and record tables."""

    _connection: sqlite3.Connection
    _meta_table: str
    _record_table: str

    def _bootstrap_schema(self) -> None:
        # Schema creation, singleton bootstrap, and migrations share one
        # BEGIN IMMEDIATE lock. Concurrent constructors therefore serialize
        # before any metadata is inspected or mutated.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._create_tables()
            columns = self._meta_columns()
            version = self._meta_schema_version()
            self._migrate_schema(version, columns)

            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _create_tables(self) -> None:
        self._connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._meta_table} (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                owner_id TEXT,
                owner_epoch INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._record_table} (
                collection TEXT NOT NULL,
                record_key TEXT NOT NULL,
                position INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (collection, record_key)
            )
            """
        )

    def _meta_columns(self) -> set[str]:
        return {
            str(row[1])
            for row in self._connection.execute(f"PRAGMA table_info({self._meta_table})").fetchall()
        }

    def _meta_schema_version(self) -> int:
        row = self._connection.execute(
            f"SELECT schema_version FROM {self._meta_table} WHERE singleton = 1"
        ).fetchone()
        if row is None:
            self._connection.execute(
                f"""
                INSERT OR IGNORE INTO {self._meta_table}(
                    singleton,
                    schema_version,
                    revision
                )
                VALUES (1, ?, 0)
                """,
                (_SCHEMA_VERSION,),
            )
            row = self._connection.execute(
                f"""
                SELECT schema_version
                FROM {self._meta_table}
                WHERE singleton = 1
                """
            ).fetchone()
        if row is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("SQLite incremental metadata bootstrap failed")
        return int(row[0])

    def _migrate_schema(self, version: int, columns: set[str]) -> None:
        if version == 1:
            if "revision" not in columns:
                self._connection.execute(
                    f"""
                    ALTER TABLE {self._meta_table}
                    ADD COLUMN revision INTEGER NOT NULL DEFAULT 0
                    """
                )
            version = 2
        if version == 2:
            if "owner_id" not in columns:
                self._connection.execute(f"ALTER TABLE {self._meta_table} ADD COLUMN owner_id TEXT")
            if "owner_epoch" not in columns:
                self._connection.execute(
                    f"""
                    ALTER TABLE {self._meta_table}
                    ADD COLUMN owner_epoch INTEGER NOT NULL DEFAULT 0
                    """
                )
            self._connection.execute(
                f"""
                UPDATE {self._meta_table}
                SET schema_version = 3
                WHERE singleton = 1
                """
            )
            return
        if version != _SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported SQLiteIncrementalPersistence schema version: {version}"
            )
