"""Schema probe, bootstrap DDL and v1->v2 migration of PostgresPersistence (from postgres.py)."""

from __future__ import annotations

import psycopg
from psycopg import sql


_SCHEMA_VERSION = 2


def committed_schema_is_current(
    connection: psycopg.Connection,
    namespace: str,
    meta_table: sql.Identifier,
) -> bool:
    """Probe, without DDL or advisory locks, for an already committed namespace."""
    relations = connection.execute(
        "SELECT to_regclass(%s), to_regclass(%s), to_regclass(%s), to_regclass(%s)",
        (
            f"{namespace}_record_meta",
            f"{namespace}_record",
            f"{namespace}_writer_scope",
            f"{namespace}_recovery_owner",
        ),
    ).fetchone()
    if relations is not None and all(relation is not None for relation in relations):
        committed_meta = connection.execute(
            sql.SQL("SELECT schema_version FROM {} WHERE singleton = 1").format(meta_table)
        ).fetchone()
        if committed_meta is not None and int(committed_meta[0]) == _SCHEMA_VERSION:
            return True
    return False


def bootstrap_schema(
    connection: psycopg.Connection,
    namespace: str,
    *,
    meta_table: sql.Identifier,
    record_table: sql.Identifier,
    scoped_writers_table: sql.Identifier,
    correlation_owners_table: sql.Identifier,
) -> None:
    """Create or migrate the namespace tables under the exclusive namespace lock."""
    with connection.transaction():
        # CREATE TABLE IF NOT EXISTS does not serialize concurrent first
        # creation of PostgreSQL composite types/catalog rows. Use the
        # same namespace advisory lock as boundary/epoch writers, in its
        # exclusive form, before any schema creation or migration.
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (namespace,),
        )
        connection.execute(
            sql.SQL(
                """
                    CREATE TABLE IF NOT EXISTS {} (
                        singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        revision BIGINT NOT NULL DEFAULT 0,
                        owner_id TEXT,
                        owner_epoch BIGINT NOT NULL DEFAULT 0
                    )
                    """
            ).format(meta_table)
        )
        connection.execute(
            sql.SQL(
                """
                    CREATE TABLE IF NOT EXISTS {} (
                        collection TEXT NOT NULL,
                        record_key TEXT NOT NULL,
                        position BIGINT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (collection, record_key)
                    )
                    """
            ).format(record_table)
        )
        connection.execute(
            sql.SQL(
                """
                    INSERT INTO {}(singleton, schema_version, revision)
                    VALUES (1, %s, 0)
                    ON CONFLICT(singleton) DO NOTHING
                    """
            ).format(meta_table),
            (_SCHEMA_VERSION,),
        )
        connection.execute(
            sql.SQL(
                """
                    CREATE TABLE IF NOT EXISTS {} (
                        scope TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        owner_epoch BIGINT NOT NULL CHECK (owner_epoch >= 1)
                    )
                    """
            ).format(scoped_writers_table)
        )
        connection.execute(
            sql.SQL(
                """
                    CREATE TABLE IF NOT EXISTS {} (
                        correlation_id TEXT PRIMARY KEY,
                        scope TEXT NOT NULL UNIQUE
                    )
                    """
            ).format(correlation_owners_table)
        )
        row = connection.execute(
            sql.SQL("SELECT schema_version FROM {} WHERE singleton = 1").format(meta_table)
        ).fetchone()
        if row is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("PostgresPersistence metadata bootstrap failed")
        version = int(row[0])
        if version == 1:
            connection.execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS owner_id TEXT").format(meta_table)
            )
            connection.execute(
                sql.SQL(
                    "ALTER TABLE {} ADD COLUMN IF NOT EXISTS owner_epoch BIGINT NOT NULL DEFAULT 0"
                ).format(meta_table)
            )
            connection.execute(
                sql.SQL("UPDATE {} SET schema_version = 2 WHERE singleton = 1").format(meta_table)
            )
            version = 2
        if version != _SCHEMA_VERSION:
            raise RuntimeError(f"unsupported PostgresPersistence schema version: {version}")
