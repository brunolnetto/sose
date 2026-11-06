"""Read snapshot opening and repeated point-read costs."""
from __future__ import annotations

from datetime import datetime, timezone
import os

import pytest

from sose.domain.entity import Entity
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
SIZES = [100, 1_000] if QUICK else [1_000, 10_000]
READ_COUNTS = [1, 10] if QUICK else [1, 10, 100]
ROUNDS = 1 if QUICK else 5
NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)

ADAPTERS = (
    pytest.param(MemoryPersistence, None, id="memory"),
    pytest.param(SQLitePersistence, ".sqlite3", id="sqlite-snapshot"),
    pytest.param(SQLiteIncrementalPersistence, ".sqlite3", id="sqlite-incremental"),
    pytest.param(JSONLJournalPersistence, ".jsonl", id="jsonl-journal"),
    pytest.param(DuckDBPersistence, ".duckdb", id="duckdb"),
)


def _open(factory, tmp_path, suffix):
    return factory() if suffix is None else factory(tmp_path / f"state{suffix}")


def _close(persistence):
    close = getattr(persistence, "close", None)
    if callable(close):
        close()


def _seed(persistence, size: int) -> None:
    with persistence.transaction() as uow:
        for index in range(size):
            entity_id = f"entity-{index:06d}"
            uow.save_entity(
                Entity(
                    id=entity_id,
                    entity_type="benchmark",
                    state="ready",
                    attributes={"ordinal": index},
                    created_at=NOW,
                    updated_at=NOW,
                    version=1,
                )
            )


@pytest.mark.parametrize("factory,suffix", ADAPTERS)
@pytest.mark.parametrize("size", SIZES)
def test_persistence_read_snapshot_open(benchmark, tmp_path, factory, suffix, size):
    persistence = _open(factory, tmp_path, suffix)
    try:
        _seed(persistence, size)

        def open_snapshot():
            with persistence.read_snapshot():
                pass

        benchmark.pedantic(open_snapshot, rounds=ROUNDS, iterations=1)

        with persistence.read_snapshot() as snapshot:
            assert len(snapshot.entities()) == size

        benchmark.extra_info["entity_count"] = size
    finally:
        _close(persistence)


@pytest.mark.parametrize("factory,suffix", ADAPTERS)
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("read_count", READ_COUNTS)
def test_persistence_repeated_point_reads_in_snapshot(
    benchmark, tmp_path, factory, suffix, size, read_count
):
    persistence = _open(factory, tmp_path, suffix)
    try:
        _seed(persistence, size)
        target = f"entity-{size // 2:06d}"

        def read_repeatedly():
            with persistence.read_snapshot() as snapshot:
                values = [
                    snapshot.entity("benchmark", target)
                    for _ in range(read_count)
                ]
                assert all(value is not None and value.id == target for value in values)

        benchmark.pedantic(read_repeatedly, rounds=ROUNDS, iterations=1)

        with persistence.read_snapshot() as snapshot:
            value = snapshot.entity("benchmark", target)
            assert value is not None
            assert value.id == target

        benchmark.extra_info["entity_count"] = size
        benchmark.extra_info["reads_per_snapshot"] = read_count
    finally:
        _close(persistence)
