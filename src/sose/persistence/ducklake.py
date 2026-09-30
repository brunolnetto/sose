from __future__ import annotations

from pathlib import Path

import duckdb

from .duckdb import DuckDBPersistence


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

    def _initialize_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sose_record_meta (
                singleton INTEGER PRIMARY KEY,
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
                PRIMARY KEY (collection, record_key)
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
