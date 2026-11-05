"""Compare batch command-existence lookup with the legacy per-ID fallback.

Run from the repository root with:
    uv run --python 3.14 python benchmarks/batch_command_existence.py

This stdlib-only microbenchmark deliberately avoids the pytest-benchmark
dependency introduced by PR #605, keeping the #607 implementation branch
independently runnable. Persistence setup and command seeding are excluded
from the timed region.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from statistics import median
from timeit import repeat

from sose.core.events import Command
from sose.persistence.memory import MemoryPersistence


def _seed_commands(persistence: MemoryPersistence, command_count: int) -> None:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-count", type=int, default=10_000)
    parser.add_argument(
        "--lookup-counts", type=int, nargs="+", default=[100, 500, 1_000]
    )
    parser.add_argument("--number", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=7)
    args = parser.parse_args()

    if args.command_count < 1 or any(
        count < 1 for count in args.lookup_counts
    ):
        parser.error("command and lookup counts must be positive")
    if args.number < 1 or args.rounds < 1:
        parser.error("--number and --rounds must be positive")

    persistence = MemoryPersistence()
    try:
        _seed_commands(persistence, args.command_count)
        print(
            "lookup_count\tbatch_median_us\tper_id_median_us\t"
            "fallback_over_batch"
        )
        for lookup_count in args.lookup_counts:
            present_count = (lookup_count * 4) // 5
            if present_count > args.command_count:
                parser.error(
                    f"--command-count must be >= {present_count} "
                    f"for lookup count {lookup_count}"
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
                batch, number=args.number, rounds=args.rounds
            )
            per_id_us = _median_us_per_call(
                per_id, number=args.number, rounds=args.rounds
            )
            ratio = per_id_us / batch_us if batch_us else float("inf")
            print(
                f"{lookup_count}\t{batch_us:.3f}\t{per_id_us:.3f}\t{ratio:.2f}x"
            )
    finally:
        close = getattr(persistence, "close", None)
        if close is not None:
            close()


if __name__ == "__main__":
    main()
