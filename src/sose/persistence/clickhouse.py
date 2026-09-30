from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from .memory import MemoryPersistence, MemoryUnitOfWork


class ClickHousePersistence(MemoryPersistence):
    """ClickHouse analytical persistence boundary.

    The initial adapter is intentionally not authoritative. ClickHouse receives
    the SOSE canonical state projection for analytical/replay use while the
    authoritative conformance work defines safe mutation semantics separately.
    """

    def __init__(self, *, host: str, database: str = "default") -> None:
        super().__init__()
        import clickhouse_connect

        self._client = clickhouse_connect.get_client(host=host, database=database)

    def close(self) -> None:
        self._client.close()

    @contextmanager
    def transaction(self) -> Iterator[MemoryUnitOfWork]:
        # Memory UoW preserves SOSE semantics for the runtime. Persisting the
        # analytical projection is intentionally separated from authority until
        # ClickHouse-specific atomicity/ownership guarantees are qualified.
        with super().transaction() as uow:
            yield uow
