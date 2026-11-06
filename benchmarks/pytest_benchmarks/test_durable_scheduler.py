"""Component benchmarks for durable scheduled-work queries and cancellation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest

from sose.core.durable import DurableScheduler
from sose.core.events import Command
from sose.core.runtime import ScheduledWork
from sose.persistence.memory import MemoryPersistence


QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
ROUNDS = 1 if QUICK else 5
SIZES = [10, 100] if QUICK else [100, 1_000, 10_000]
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNER_ID = "benchmark-durable-scheduler"


def _seed_pending(persistence: MemoryPersistence, total_count: int, due_count: int):
    with persistence.transaction(owner_id=OWNER_ID) as uow:
        for index in range(total_count):
            command_id = f"command-{index:07d}"
            entity_id = f"entity-{index:07d}"
            due_at = ORIGIN if index < due_count else ORIGIN + timedelta(hours=1)
            command = Command(
                command_id=command_id,
                name="release",
                entity_type="benchmark",
                entity_id=entity_id,
                due_at=due_at,
                owner_id=OWNER_ID,
            )
            work = ScheduledWork(
                work_id=f"work-{index:07d}",
                due_at=due_at,
                priority=index % 7,
                sequence=index + 1,
                command_id=command_id,
                owner_id=OWNER_ID,
            )
            uow.save_command(command)
            uow.save_scheduled_work(work)


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("due_fraction", [0.0, 0.5, 1.0])
def test_durable_scheduler_due_future_ratio(benchmark, size, due_fraction):
    due_count = int(size * due_fraction)
    persistence = MemoryPersistence()
    _seed_pending(persistence, size, due_count)
    scheduler = DurableScheduler(persistence, owner_id=OWNER_ID)

    benchmark.pedantic(
        lambda: scheduler.due(ORIGIN),
        rounds=ROUNDS,
        iterations=1,
    )

    due_items = scheduler.due(ORIGIN)
    assert len(due_items) == due_count
    assert len(scheduler.pending()) == size
    benchmark.extra_info["total_scheduled_work"] = size
    benchmark.extra_info["due_work"] = due_count
    benchmark.extra_info["future_work"] = size - due_count
    benchmark.extra_info["due_fraction"] = due_fraction


@pytest.mark.parametrize("size", SIZES)
def test_durable_scheduler_find_pending_scaling(benchmark, size):
    persistence = MemoryPersistence()
    _seed_pending(persistence, size, size)
    scheduler = DurableScheduler(persistence, owner_id=OWNER_ID)
    target_id = f"entity-{size - 1:07d}"

    benchmark.pedantic(
        lambda: scheduler.find_pending(
            entity_type="benchmark",
            entity_id=target_id,
            name="release",
        ),
        rounds=ROUNDS,
        iterations=1,
    )

    found = scheduler.find_pending(
        entity_type="benchmark",
        entity_id=target_id,
        name="release",
    )
    assert found is not None
    assert found.command.entity_id == target_id
    benchmark.extra_info["total_scheduled_work"] = size
    benchmark.extra_info["target_position"] = size - 1


@pytest.mark.parametrize("size", SIZES)
def test_durable_scheduler_cancel_scaling(benchmark, size):
    persistence = MemoryPersistence()
    _seed_pending(persistence, size, 0)
    scheduler = DurableScheduler(persistence, owner_id=OWNER_ID)
    work_ids = [f"work-{index:07d}" for index in range(ROUNDS)]
    cursor = 0
    cancelled = []

    def cancel_next():
        nonlocal cursor
        cancelled.append(scheduler.cancel(work_ids[cursor]))
        cursor += 1

    benchmark.pedantic(cancel_next, rounds=ROUNDS, iterations=1)

    assert all(cancelled)
    assert len(scheduler.pending()) == size - ROUNDS
    benchmark.extra_info["total_scheduled_work_before"] = size
    benchmark.extra_info["cancel_operations"] = ROUNDS
