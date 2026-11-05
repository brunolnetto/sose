"""Measure batch command lookup versus per-ID refreshes on durable adapters.

Run from the repository root with:
    uv run --python 3.14 python benchmarks/persistent_batch_command_existence.py

The benchmark seeds each adapter once, then times only the lookup operation.
It intentionally uses the stdlib timeit module so PR #607 does not depend on
the pytest-benchmark harness proposed independently in PR #605.

PostgreSQL is excluded because it requires an explicitly provisioned service;
benchmark it in a separate environment with controlled server and network
conditions rather than silently treating an in-memory or local adapter as a
proxy for it.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from timeit import repeat

from sose.core.events import Command
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


ADAPTERS = (
    ("sqlite-snapshot", SQLitePersistence, ".sqlite"),
    ("sqlite-incremental", SQLiteIncrementalPersistence, ".sqlite"),
    ("duckdb", DuckDBPersistence, ".duckdb"),
    ("jsonl-journal", JSONLJournalPersistence, ".jsonl"),
)


def _seed_commands(persistence, command_count: int) -> None:
    due_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with persistence.transaction() as uow:
        for index in range(command_count):
            uow.save_command(
                Command(
                    command_id=f"command-{index}",
                    name="benchmark",
                    entity_type="benchmark",
                    entity_id=f"entity-{index}",
                    due_at=due_at,
                    owner_id="benchmark-owner",
                )
            )


def _median_us_per_call(operation, *, number: int, rounds: int) -> float:
    samples = repeat(operation, number=number, repeat=rounds)
    return median(sample / number for sample in samples) * 1_000_000


def _benchmark_adapter(
    name: str,
    adapter_type,
    path: Path,
    *,
    command_count: int,
    lookup_counts: tuple[int, ...],
    number: int,
    rounds: int,
) -> None:
    persistence = adapter_type(path)
    try:
        _seed_commands(persistence, command_count)
        print(f"\n[{name}]")
        print(
            "lookup_count\tbatch_median_us\tper_id_median_us\t"
            "fallback_over_batch"
        )
        for lookup_count in lookup_counts:
            present_count = (lookup_count * 4) // 5
            if present_count > command_count:
                raise ValueError(
                    f"command_count ({command_count}) is too small for "
                    f"{lookup_count} lookups"
                )
            command_ids = tuple(
                [f"command-{index}" for index in range(present_count)]
                + [
                    f"missing-{index}"
                    for index in range(lookup_count - present_count)
                ]
            )

            def batch():
                return persistence.missing_command_ids(command_ids)

            def per_id():
                return frozenset(
                    command_id
                    for command_id in command_ids
                    if persistence.command(command_id) is None
                )
            expected = batch()
            assert per_id() == expected

            batch_us = _median_us_per_call(
                batch, number=number, rounds=rounds
            )
            per_id_us = _median_us_per_call(
                per_id, number=number, rounds=rounds
            )
            ratio = per_id_us / batch_us if batch_us else float("inf")
            print(
                f"{lookup_count}\t{batch_us:.3f}\t{per_id_us:.3f}\t{ratio:.2f}x"
            )
    finally:
        close = getattr(persistence, "close", None)
        if close is not None:
            close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-count", type=int, default=1_000)
    parser.add_argument("--lookup-counts", type=int, nargs="+", default=[100, 500])
    parser.add_argument("--number", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()

    if args.command_count < 1 or any(count < 1 for count in args.lookup_counts):
        parser.error("command and lookup counts must be positive")
    if args.number < 1 or args.rounds < 1:
        parser.error("--number and --rounds must be positive")

    with TemporaryDirectory(prefix="sose-batch-command-bench-") as directory:
        root = Path(directory)
        for name, adapter_type, suffix in ADAPTERS:
            _benchmark_adapter(
                name,
                adapter_type,
                root / f"{name}{suffix}",
                command_count=args.command_count,
                lookup_counts=tuple(args.lookup_counts),
                number=args.number,
                rounds=args.rounds,
            )


if __name__ == "__main__":
    main()
