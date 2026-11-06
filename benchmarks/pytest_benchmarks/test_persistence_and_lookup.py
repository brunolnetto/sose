"""Persistence scaling and command-existence microbenchmarks."""
from __future__ import annotations
from datetime import datetime, timezone
import os
from pathlib import Path
import pytest
from sose.core.events import Command
from sose.domain.entity import Entity
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
SIZES = [10, 100] if QUICK else [10, 100, 1_000, 10_000]
LOOKUPS = [100, 500] if QUICK else [100, 500, 1_000]
PERSISTENT_LOOKUPS = [100, 500]
COMMANDS = 1_000 if QUICK else 10_000
SINKS = (
    pytest.param(SQLitePersistence, ".sqlite3", id="sqlite-snapshot"),
    pytest.param(SQLiteIncrementalPersistence, ".sqlite3", id="sqlite-incremental"),
    pytest.param(JSONLJournalPersistence, ".jsonl", id="jsonl-journal"),
    pytest.param(DuckDBPersistence, ".duckdb", id="duckdb"),
)
NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)

def _entity(i):
    return Entity(id=f"entity-{i:06d}", entity_type="scaling", state="ready",
                  attributes={"ordinal": i, "updates": 0}, created_at=NOW, updated_at=NOW, version=1)

def _seed_entities(persistence, count):
    with persistence.transaction() as uow:
        for i in range(count): uow.save_entity(_entity(i))

def _update_one(persistence, target, iteration):
    with persistence.transaction() as uow:
        entity = uow.get_entity("scaling", target)
        assert entity is not None
        entity.attributes["updates"] = iteration + 1
        entity.version += 1
        entity.updated_at = NOW
        uow.save_entity(entity)

def _seed_commands(persistence, count):
    due = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with persistence.transaction() as uow:
        for i in range(count):
            uow.save_command(Command(command_id=f"command-{i}", name="benchmark",
                entity_type="benchmark", entity_id=f"entity-{i}", due_at=due, owner_id="benchmark-owner"))

def _lookup_ids(count):
    present = count * 4 // 5
    return tuple([f"command-{i}" for i in range(present)] +
                 [f"missing-{i}" for i in range(count - present)])

def _batch(persistence, ids): return persistence.missing_command_ids(ids)
def _per_id(persistence, ids):
    return frozenset(command_id for command_id in ids if persistence.command(command_id) is None)

@pytest.mark.parametrize("factory,suffix", SINKS)
@pytest.mark.parametrize("size", SIZES)
def test_persistence_single_record_update_scaling(benchmark, tmp_path, factory, suffix, size):
    path = tmp_path / f"state{suffix}"
    persistence = factory(path)
    _seed_entities(persistence, size)
    close = getattr(persistence, "close", None)
    if callable(close): close()
    persistence = factory(path)  # reopen: exclude setup caches
    target = f"entity-{size // 2:06d}"
    iteration = 0
    def update():
        nonlocal iteration
        _update_one(persistence, target, iteration)
        iteration += 1
    try:
        benchmark.pedantic(update, rounds=1 if QUICK else 5, iterations=1)
        benchmark.extra_info["storage_bytes"] = sum(
            p.stat().st_size for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")) if p.exists())
        assert persistence.get_entity("scaling", target) is not None
    finally:
        close = getattr(persistence, "close", None)
        if callable(close): close()

@pytest.mark.parametrize("lookup_count", LOOKUPS)
@pytest.mark.parametrize("operation", ["batch", "per_id"])
def test_memory_command_existence_lookup(benchmark, lookup_count, operation):
    persistence = MemoryPersistence()
    _seed_commands(persistence, COMMANDS)
    ids = _lookup_ids(lookup_count)
    expected = _batch(persistence, ids)
    assert _per_id(persistence, ids) == expected
    fn = _batch if operation == "batch" else _per_id
    benchmark.pedantic(lambda: fn(persistence, ids), rounds=1 if QUICK else 7,
                       iterations=1 if QUICK else 100)
    assert fn(persistence, ids) == expected

@pytest.mark.parametrize("adapter,suffix", SINKS)
@pytest.mark.parametrize("lookup_count", PERSISTENT_LOOKUPS)
@pytest.mark.parametrize("operation", ["batch", "per_id"])
def test_persistent_command_existence_lookup(benchmark, tmp_path, adapter, suffix, lookup_count, operation):
    persistence = adapter(tmp_path / f"commands{suffix}")
    try:
        _seed_commands(persistence, COMMANDS)
        ids = _lookup_ids(lookup_count)
        expected = _batch(persistence, ids)
        assert _per_id(persistence, ids) == expected
        fn = _batch if operation == "batch" else _per_id
        benchmark.pedantic(lambda: fn(persistence, ids), rounds=1 if QUICK else 5,
                           iterations=1 if QUICK else 3)
        assert fn(persistence, ids) == expected
    finally:
        close = getattr(persistence, "close", None)
        if callable(close): close()
