from __future__ import annotations

from pathlib import Path

import duckdb

from .duckdb import DuckDBPersistence
from .records import changes_for_dirty_records


class DuckLakePersistence(DuckDBPersistence):
    """SOSE record persistence backed by a DuckLake catalog.

    DuckLake is treated as a durable candidate only. Authoritative promotion
    requires the executable SOSE qualification suite for the selected catalog
    and storage configuration.
    """

    def __init__(
        self,
        catalog: str | Path,
        *,
        data_path: str | Path,
        alias: str = "sose_ducklake",
    ) -> None:
        # Build the inherited object without opening its local DuckDB database.
        self.path = str(catalog)
        catalog_path = Path(catalog)
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        data_path = Path(data_path)
        data_path.mkdir(parents=True, exist_ok=True)
        self._connection = duckdb.connect(":memory:")
        self._connection.execute("INSTALL ducklake")
        self._connection.execute("LOAD ducklake")
        self._connection.execute(
            f"ATTACH ? AS {alias} (TYPE DUCKLAKE, DATA_PATH ?)",
            [str(catalog), str(data_path)],
        )
        self._connection.execute(f"USE {alias}")
        self._initialize_schema()
        self._refresh_from_db()

    def _apply_changes(self, before, after, dirty_records) -> int:
        changes = changes_for_dirty_records(before, after, dirty_records)
        for change in changes:
            self._connection.execute(
                "DELETE FROM sose_record WHERE collection = ? AND record_key = ?",
                [change.collection, change.key],
            )
            if change.operation == "upsert":
                if change.position is None or change.payload is None:
                    raise RuntimeError("upsert change requires position and payload")
                self._connection.execute(
                    """
                    INSERT INTO sose_record(collection, record_key, position, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    [change.collection, change.key, change.position, change.payload],
                )
            elif change.operation != "delete":
                raise RuntimeError(f"unknown state record operation: {change.operation}")
        return len(changes)

    def _initialize_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sose_record_meta (
                singleton INTEGER NOT NULL,
                schema_version INTEGER NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sose_record (
                collection VARCHAR NOT NULL,
                record_key VARCHAR NOT NULL,
                position BIGINT NOT NULL,
                payload VARCHAR NOT NULL,
            )
            """
        )
        row = self._connection.execute(
            "SELECT schema_version FROM sose_record_meta WHERE singleton = 1"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO sose_record_meta VALUES (1, 1)"
            )
        elif int(row[0]) != 1:
            raise RuntimeError(
                f"unsupported DuckLakePersistence schema version: {row[0]}"
            )
