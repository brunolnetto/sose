"""Persistence transaction cost as the dirty entity set grows."""
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
SIZES = [100, 1_000] if QUICK else [100, 1_000, 10_000]
DIRTY_COUNTS = [1, 10] if QUICK else [1, 10, 100, 1_000]
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


def _entity(index: int) -> Entity:
    return Entity(
        id=f"entity-{index:06d}",
        entity_type="benchmark",
        state="ready",
        attributes={"ordinal": index, "updates": 0},
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )


def _seed(persistence, size: int) -> None:
    with persistence.transaction() as uow:
        for index in range(size):
            uow.save_entity(_entity(index))


def _update_many(persistence, dirty_count: int, iteration: int) -> None:
    with persistence.transaction() as uow:
        for index in range(dirty_count):
            entity = uow.get_entity("benchmark", f"entity-{index:06d}")
            assert entity is not None
            entity.attributes["updates"] = iteration + 1
            entity.version += 1
            entity.updated_at = NOW
            uow.save_entity(entity)


@pytest.mark.parametrize("factory,suffix", ADAPTERS)
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("dirty_count", DIRTY_COUNTS)
def test_persistence_transaction_dirty_set_scaling(
    benchmark, tmp_path, factory, suffix, size, dirty_count
):
    if dirty_count > size:
        pytest.skip("dirty set cannot exceed seeded entity count")

    persistence = _open(factory, tmp_path, suffix)
    try:
        _seed(persistence, size)
        iteration = 0

        def update():
            nonlocal iteration
            _update_many(persistence, dirty_count, iteration)
            iteration += 1

        benchmark.pedantic(update, rounds=ROUNDS, iterations=1)

        for index in range(dirty_count):
            entity = persistence.entity("benchmark", f"entity-{index:06d}")
            assert entity is not None
            assert entity.attributes["updates"] == iteration
        untouched = persistence.entity("benchmark", f"entity-{size - 1:06d}")
        if size > dirty_count:
            assert untouched is not None
            assert untouched.attributes["updates"] == 0

        benchmark.extra_info["total_entities"] = size
        benchmark.extra_info["dirty_entities"] = dirty_count
        benchmark.extra_info["dirty_fraction"] = dirty_count / size
    finally:
        _close(persistence)
