from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from .memory import MemoryPersistence, MemoryUnitOfWork
from .records import state_to_records


class ClickHousePersistence(MemoryPersistence):
    """ClickHouse analytical projection of the canonical SOSE record state.

    Runtime authority remains in memory. After each successful SOSE transaction,
    the complete canonical record projection is published to ClickHouse for
    analytical/replay inspection. This adapter intentionally does not claim
    restart reconstruction or authoritative transaction semantics.
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
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                snapshot UInt64,
                collection String,
                record_key String,
                position Int64,
                payload String
            )
            ENGINE = MergeTree
            ORDER BY (snapshot, collection, position, record_key)
            """
        )
        self._snapshot = 0

    def close(self) -> None:
        self._client.close()

    def _publish_snapshot(self) -> None:
        self._snapshot += 1
        rows = [
            [self._snapshot, record.collection, record.key, record.position, record.payload]
            for record in state_to_records(self._state)
        ]
        if rows:
            self._client.insert(
                self._table,
                rows,
                column_names=[
                    "snapshot",
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
        self._publish_snapshot()
