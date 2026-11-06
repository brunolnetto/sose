"""End-to-end runtime workloads measured through pytest-benchmark."""
from __future__ import annotations
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import pytest
from sose.backends.simpy import SimPyBackend
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.core.stores import DurableStoreManager
from sose.examples.tutorial_job.simulation import ORIGIN, build_runtime, seed_job, start_and_schedule_completion
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
ROUNDS = 1 if QUICK else 5

def _memory_scheduled_batch(count):
    persistence = MemoryPersistence()
    owner = "benchmark-memory"
    jobs = [seed_job(persistence, owner_id=owner, job_key=f"memory-{i}") for i in range(count)]
    _, engine = build_runtime(persistence, owner_id=owner)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    due = ORIGIN + timedelta(hours=1)
    for job in jobs:
        start_and_schedule_completion(persistence, engine, job_id=job.id, complete_at=due)
    backend.run_until(due)
    assert not persistence.scheduled_work()

def _file_sink_scheduled_batch(count, factory, filename, prefix):
    with tempfile.TemporaryDirectory() as directory:
        persistence = factory(Path(directory) / filename)
        owner = f"benchmark-{prefix}"
        jobs = [seed_job(persistence, owner_id=owner, job_key=f"{prefix}-{i}") for i in range(count)]
        _, engine = build_runtime(persistence, owner_id=owner)
        backend = SimPyBackend(origin=ORIGIN)
        engine.rebuild_backend(backend)
        due = ORIGIN + timedelta(hours=1)
        for job in jobs:
            start_and_schedule_completion(persistence, engine, job_id=job.id, complete_at=due)
        backend.run_until(due)
        assert not persistence.scheduled_work()
        close = getattr(persistence, "close", None)
        if callable(close):
            close()

def _file_sink_reopen(count, factory, filename, prefix):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / filename
        persistence = factory(path)
        owner = f"benchmark-{prefix}"
        jobs = [seed_job(persistence, owner_id=owner, job_key=f"{prefix}-{i}") for i in range(count)]
        _, engine = build_runtime(persistence, owner_id=owner)
        backend = SimPyBackend(origin=ORIGIN)
        engine.rebuild_backend(backend)
        due = ORIGIN + timedelta(hours=2)
        for job in jobs:
            start_and_schedule_completion(persistence, engine, job_id=job.id, complete_at=due)
        close = getattr(persistence, "close", None)
        if callable(close):
            close()
        reopened = factory(path)
        try:
            _, rebuilt = build_runtime(reopened, owner_id=owner, now=ORIGIN)
            rebuilt.rebuild_backend(SimPyBackend(origin=ORIGIN))
            assert len(reopened.scheduled_work()) == count
        finally:
            close = getattr(reopened, "close", None)
            if callable(close):
                close()

def _diagnostics_collection(count):
    persistence = MemoryPersistence()
    owner = "benchmark-diagnostics"
    jobs = [seed_job(persistence, owner_id=owner, job_key=f"diagnostics-{i}") for i in range(count)]
    _, engine = build_runtime(persistence, owner_id=owner)
    engine.rebuild_backend(SimPyBackend(origin=ORIGIN))
    due = ORIGIN + timedelta(hours=4)
    for job in jobs:
        start_and_schedule_completion(persistence, engine, job_id=job.id, complete_at=due)
    assert engine.diagnostics().counts.scheduled_work == count

def _resource_contention(count):
    persistence = MemoryPersistence()
    manager = DurableResourceManager(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("workers", capacity=8))
    manager.rebuild_backend(backend)
    for i in range(count):
        manager.request(backend, resource_name="workers", request_id=f"request-{i:05d}",
                        requested_at=ORIGIN, priority=i % 5)
    backend.run_until(ORIGIN)
    while persistence.resource_reservations():
        for reservation in tuple(persistence.resource_reservations()):
            manager.release(backend, reservation.reservation_id)
        backend.run_until(ORIGIN)
    assert not persistence.resource_demands()
    assert not persistence.resource_reservations()

def _priority_store_selection(count):
    persistence = MemoryPersistence()
    manager = DurableStoreManager(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    manager.define(StoreDefinition("dispatch", kind="priority"))
    manager.rebuild_backend(backend)
    for i in range(count):
        manager.put(backend, store_name="dispatch", item_id=f"item-{i:05d}", value=i,
                    priority=i % 11, requested_at=ORIGIN)
    backend.run_until(ORIGIN)
    for i in range(count):
        assert manager.ensure_selection(backend, store_name="dispatch", request_id=f"pick-{i:05d}",
                                        requested_at=ORIGIN) is not None
    assert not persistence.store_items()
    assert not persistence.store_get_requests()

ADAPTERS = (
    pytest.param("sqlite", SQLitePersistence, "snapshot.sqlite3", id="sqlite-snapshot"),
    pytest.param("sqlite-incremental", SQLiteIncrementalPersistence, "incremental.sqlite3", id="sqlite-incremental"),
    pytest.param("jsonl", JSONLJournalPersistence, "journal.jsonl", id="jsonl-journal"),
    pytest.param("duckdb", DuckDBPersistence, "state.duckdb", id="duckdb"),
)

@pytest.mark.parametrize("workload,size", [
    (_memory_scheduled_batch, 24 if QUICK else 500),
    (lambda n: _file_sink_scheduled_batch(n, SQLitePersistence, "snapshot.sqlite3", "sqlite-snapshot"), 8 if QUICK else 100),
    (lambda n: _file_sink_scheduled_batch(n, SQLiteIncrementalPersistence, "incremental.sqlite3", "sqlite-incremental"), 8 if QUICK else 100),
    (lambda n: _file_sink_scheduled_batch(n, JSONLJournalPersistence, "journal.jsonl", "jsonl-journal"), 8 if QUICK else 100),
    (lambda n: _file_sink_scheduled_batch(n, DuckDBPersistence, "state.duckdb", "duckdb"), 8 if QUICK else 100),
    (_diagnostics_collection, 24 if QUICK else 500),
    (_resource_contention, 32 if QUICK else 500),
    (_priority_store_selection, 32 if QUICK else 500),
])
def test_runtime_workload(benchmark, workload, size):
    benchmark.pedantic(lambda: workload(size), rounds=ROUNDS, iterations=1)

@pytest.mark.parametrize("name,factory,filename", ADAPTERS)
@pytest.mark.parametrize("size", [8, 100] if QUICK else [100])
def test_persistent_reopen_workload(benchmark, name, factory, filename, size):
    benchmark.pedantic(lambda: _file_sink_reopen(size, factory, filename, name),
                       rounds=ROUNDS, iterations=1)
