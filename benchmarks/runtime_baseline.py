from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import timedelta
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
from time import perf_counter

from sose.backends.simpy import SimPyBackend
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.core.stores import DurableStoreManager
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    name: str
    items: int
    repetitions: int
    seconds_median: float
    seconds_min: float
    seconds_max: float
    items_per_second_median: float


def _measure(name, items, repetitions, fn) -> BenchmarkResult:
    durations = []
    for _ in range(repetitions):
        started = perf_counter()
        fn()
        durations.append(perf_counter() - started)
    median = statistics.median(durations)
    return BenchmarkResult(
        name=name,
        items=items,
        repetitions=repetitions,
        seconds_median=median,
        seconds_min=min(durations),
        seconds_max=max(durations),
        items_per_second_median=(items / median) if median else float("inf"),
    )


def _memory_scheduled_batch(count: int) -> None:
    persistence = MemoryPersistence()
    jobs = [seed_job(persistence, job_key=f"memory-{index}") for index in range(count)]
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    due_at = ORIGIN + timedelta(hours=1)

    for job in jobs:
        start_and_schedule_completion(
            persistence,
            engine,
            job_id=job.id,
            complete_at=due_at,
        )
    backend.run_until(due_at)

    if persistence.scheduled_work():
        raise RuntimeError("memory scheduled benchmark left pending work")


def _sqlite_scheduled_batch(count: int) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "benchmark.sqlite3"
        persistence = SQLitePersistence(path)
        jobs = [
            seed_job(persistence, job_key=f"sqlite-{index}")
            for index in range(count)
        ]
        _, engine = build_runtime(persistence)
        backend = SimPyBackend(origin=ORIGIN)
        engine.rebuild_backend(backend)
        due_at = ORIGIN + timedelta(hours=1)

        for job in jobs:
            start_and_schedule_completion(
                persistence,
                engine,
                job_id=job.id,
                complete_at=due_at,
            )
        backend.run_until(due_at)
        if persistence.scheduled_work():
            raise RuntimeError("SQLite scheduled benchmark left pending work")
        persistence.close()


def _sqlite_reopen(count: int) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "reopen.sqlite3"
        persistence = SQLitePersistence(path)
        jobs = [
            seed_job(persistence, job_key=f"reopen-{index}")
            for index in range(count)
        ]
        _, engine = build_runtime(persistence)
        backend = SimPyBackend(origin=ORIGIN)
        engine.rebuild_backend(backend)
        due_at = ORIGIN + timedelta(hours=2)
        for job in jobs:
            start_and_schedule_completion(
                persistence,
                engine,
                job_id=job.id,
                complete_at=due_at,
            )
        persistence.close()

        reopened = SQLitePersistence(path)
        _, rebuilt = build_runtime(reopened, now=ORIGIN)
        new_backend = SimPyBackend(origin=ORIGIN)
        rebuilt.rebuild_backend(new_backend)
        if len(reopened.scheduled_work()) != count:
            raise RuntimeError("SQLite reopen benchmark lost scheduled work")
        reopened.close()


def _diagnostics_collection(count: int) -> None:
    persistence = MemoryPersistence()
    jobs = [
        seed_job(persistence, job_key=f"diagnostics-{index}")
        for index in range(count)
    ]
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    due_at = ORIGIN + timedelta(hours=4)
    for job in jobs:
        start_and_schedule_completion(
            persistence,
            engine,
            job_id=job.id,
            complete_at=due_at,
        )

    diagnostics = engine.diagnostics()
    if diagnostics.counts.scheduled_work != count:
        raise RuntimeError("diagnostics benchmark observed wrong scheduled count")


def _resource_contention(count: int) -> None:
    persistence = MemoryPersistence()
    manager = DurableResourceManager(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("workers", capacity=8))
    manager.rebuild_backend(backend)

    for index in range(count):
        manager.request(
            backend,
            resource_name="workers",
            request_id=f"request-{index:05d}",
            requested_at=ORIGIN,
            priority=index % 5,
        )
    backend.run_until(ORIGIN)

    while persistence.resource_reservations():
        for reservation in tuple(persistence.resource_reservations()):
            manager.release(backend, reservation.reservation_id)
        backend.run_until(ORIGIN)

    if persistence.resource_demands() or persistence.resource_reservations():
        raise RuntimeError("resource benchmark left durable ownership")


def _priority_store_selection(count: int) -> None:
    persistence = MemoryPersistence()
    manager = DurableStoreManager(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    manager.define(StoreDefinition("dispatch", kind="priority"))
    manager.rebuild_backend(backend)

    for index in range(count):
        manager.put(
            backend,
            store_name="dispatch",
            item_id=f"item-{index:05d}",
            value=index,
            priority=index % 11,
            requested_at=ORIGIN,
        )
    backend.run_until(ORIGIN)

    for index in range(count):
        result = manager.ensure_selection(
            backend,
            store_name="dispatch",
            request_id=f"pick-{index:05d}",
            requested_at=ORIGIN,
        )
        if result is None:
            raise RuntimeError("store benchmark failed to select an item")

    if persistence.store_items() or persistence.store_get_requests():
        raise RuntimeError("store benchmark left pending durable state")


def run_suite(*, quick: bool) -> dict[str, object]:
    repetitions = 1 if quick else 5
    sizes = {
        "memory_scheduled_batch": 24 if quick else 500,
        "sqlite_scheduled_batch": 8 if quick else 100,
        "sqlite_reopen": 8 if quick else 100,
        "diagnostics_collection": 24 if quick else 500,
        "resource_contention": 32 if quick else 500,
        "priority_store_selection": 32 if quick else 500,
    }
    cases = [
        (
            "memory_scheduled_batch",
            _memory_scheduled_batch,
        ),
        (
            "sqlite_scheduled_batch",
            _sqlite_scheduled_batch,
        ),
        (
            "sqlite_reopen",
            _sqlite_reopen,
        ),
        (
            "diagnostics_collection",
            _diagnostics_collection,
        ),
        (
            "resource_contention",
            _resource_contention,
        ),
        (
            "priority_store_selection",
            _priority_store_selection,
        ),
    ]
    results = [
        _measure(name, sizes[name], repetitions, lambda fn=fn, name=name: fn(sizes[name]))
        for name, fn in cases
    ]
    return {
        "schema_version": 1,
        "mode": "quick" if quick else "full",
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
        },
        "results": [asdict(result) for result in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = run_suite(quick=args.quick)
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
