from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import uuid4

from .memory import MemoryPersistence, MemoryUnitOfWork
from .records import state_to_records


class ClickHousePersistence(MemoryPersistence):
    """Append committed SOSE state projections to ClickHouse for analytics.

    ClickHouse is intentionally not an operational source of truth here. The
    runtime UnitOfWork remains in memory while every successful commit emits a
    complete canonical StateRecord snapshot to ClickHouse.
    """

    def __init__(
        self,
        *,
        host: str,
        database: str = "default",
        table: str = "sose_record_snapshot",
    ) -> None:
        super().__init__()
        import clickhouse_connect

        self._client = clickhouse_connect.get_client(host=host, database=database)
        self._table = table
        self._client.command(
            """
            CREATE TABLE IF NOT EXISTS {table} (
                snapshot_id String,
                collection String,
                record_key String,
                position Int64,
                payload String
            )
            ENGINE = MergeTree
            ORDER BY (snapshot_id, collection, position, record_key)
            """.format(table=self._table)
        )

    def close(self) -> None:
        self._client.close()

    def _append_snapshot(self) -> None:
        snapshot_id = str(uuid4())
        records = state_to_records(self._state).values()
        rows = [
            [snapshot_id, record.collection, record.key, record.position, record.payload]
            for record in records
        ]
        if rows:
            self._client.insert(
                self._table,
                rows,
                column_names=[
                    "snapshot_id",
                    "collection",
                    "record_key",
                    "position",
                    "payload",
                ],
            )

    @contextmanager
    def transaction(self) -> Iterator[MemoryUnitOfWork]:
        with super().transaction() as uow:
            yield uow
        self._append_snapshot()
