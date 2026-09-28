from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
from time import perf_counter
from typing import Callable

from sose.domain.entity import Entity
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class ScalingPoint:
    sink: str
    durable_entities: int
    repetitions: int
    update_seconds_median: float
    update_seconds_min: float
    update_seconds_max: float
    storage_bytes: int


Factory = Callable[[Path], object]


SINKS: dict[str, tuple[Factory, str]] = {
    "sqlite_snapshot": (SQLitePersistence, "state.sqlite3"),
    "sqlite_incremental": (SQLiteIncrementalPersistence, "state.sqlite3"),
    "jsonl": (JSONLJournalPersistence, "state.jsonl"),
    "duckdb": (DuckDBPersistence, "state.duckdb"),
}


def _entity(index: int) -> Entity:
    return Entity(
        id=f"entity-{index:06d}",
        entity_type="scaling",
        state="ready",
        attributes={"ordinal": index, "updates": 0},
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )


def _prepare(persistence, count: int) -> None:
    with persistence.transaction() as uow:
        for index in range(count):
            uow.save_entity(_entity(index))


def _storage_bytes(path: Path) -> int:
    total = 0
    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
    ):
        if candidate.exists():
            total += candidate.stat().st_size
    return total


def _time_single_record_updates(
    persistence,
    *,
    count: int,
    repetitions: int,
) -> list[float]:
    durations: list[float] = []
    target_id = f"entity-{count // 2:06d}"

    for iteration in range(repetitions):
        started = perf_counter()
        with persistence.transaction() as uow:
            entity = uow.get_entity("scaling", target_id)
            if entity is None:
                raise RuntimeError(f"missing scaling entity: {target_id}")
            entity.attributes["updates"] = iteration + 1
            entity.version += 1
            entity.updated_at = NOW
            uow.save_entity(entity)
        durations.append(perf_counter() - started)

    return durations


def measure_point(
    sink: str,
    count: int,
    repetitions: int,
) -> ScalingPoint:
    factory, filename = SINKS[sink]

    with tempfile.TemporaryDirectory(prefix=f"sose-scaling-{sink}-") as directory:
        path = Path(directory) / filename
        persistence = factory(path)
        _prepare(persistence, count)

        close = getattr(persistence, "close", None)
        if callable(close):
            close()

        # Reopen so measured updates start from durable state rather than setup
        # caches or unflushed initialization state.
        persistence = factory(path)
        durations = _time_single_record_updates(
            persistence,
            count=count,
            repetitions=repetitions,
        )

        close = getattr(persistence, "close", None)
        if callable(close):
            close()

        return ScalingPoint(
            sink=sink,
            durable_entities=count,
            repetitions=repetitions,
            update_seconds_median=statistics.median(durations),
            update_seconds_min=min(durations),
            update_seconds_max=max(durations),
            storage_bytes=_storage_bytes(path),
        )


def run_scaling(
    *,
    sizes: list[int],
    sinks: list[str],
    repetitions: int,
) -> dict[str, object]:
    points = [
        measure_point(sink, size, repetitions)
        for size in sizes
        for sink in sinks
    ]
    return {
        "schema_version": 1,
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
        },
        "sizes": sizes,
        "sinks": sinks,
        "repetitions": repetitions,
        "points": [asdict(point) for point in points],
    }


def _csv_ints(raw: str) -> list[int]:
    values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not values or any(value < 1 for value in values):
        raise argparse.ArgumentTypeError("sizes must be positive integers")
    return values


def _csv_sinks(raw: str) -> list[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = [value for value in values if value not in SINKS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown sinks: {', '.join(unknown)}"
        )
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sizes",
        type=_csv_ints,
        default=[10, 100, 1000, 10000],
        help="comma-separated durable entity cardinalities",
    )
    parser.add_argument(
        "--sinks",
        type=_csv_sinks,
        default=list(SINKS),
        help="comma-separated sink names",
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.repetitions < 1:
        parser.error("--repetitions must be >= 1")

    report = run_scaling(
        sizes=args.sizes,
        sinks=args.sinks,
        repetitions=args.repetitions,
    )
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
