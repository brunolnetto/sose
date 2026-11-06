"""Microbenchmarks for SimPy temporal operations and integrated backend rebuild."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest
from statemachine import State, StateChart

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.events import Command
from sose.core.randomness import RandomSource
from sose.core.runtime import ScheduledWork
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence


QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
ROUNDS = 1 if QUICK else 5
QUEUE_SIZES = [10, 100] if QUICK else [100, 1_000, 10_000]
RUN_UNTIL_BATCHES = [1, 10] if QUICK else [10, 100, 1_000]
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNER_ID = "benchmark-temporal-backend"


class ToggleChart(StateChart):
    idle = State(initial=True)
    busy = State()

    flip = idle.to(busy) | busy.to(idle)


@pytest.mark.parametrize("method", ["schedule_at", "schedule_after"])
@pytest.mark.parametrize("queue_size", QUEUE_SIZES)
def test_temporal_backend_schedule_insertion(benchmark, method, queue_size):
    backend = SimPyBackend(origin=ORIGIN)
    for index in range(queue_size):
        backend.schedule_at(
            ORIGIN + timedelta(days=30),
            lambda: None,
            priority=index % 7,
            key=("existing", index),
        )

    cursor = 0

    def schedule_next():
        nonlocal cursor
        key = ("new", cursor)
        if method == "schedule_at":
            backend.schedule_at(
                ORIGIN + timedelta(days=31),
                lambda: None,
                priority=cursor % 7,
                key=key,
            )
        else:
            backend.schedule_after(
                timedelta(days=31),
                lambda: None,
                priority=cursor % 7,
                key=key,
            )
        cursor += 1

    benchmark.pedantic(schedule_next, rounds=ROUNDS, iterations=1)

    assert cursor == ROUNDS
    benchmark.extra_info["existing_queue_size"] = queue_size
    benchmark.extra_info["inserted_calls"] = ROUNDS
    benchmark.extra_info["priority_buckets"] = 7


@pytest.mark.parametrize("queue_size", QUEUE_SIZES)
def test_temporal_backend_cancel(benchmark, queue_size):
    backend = SimPyBackend(origin=ORIGIN)
    calls = [
        backend.schedule_at(
            ORIGIN + timedelta(days=1),
            lambda: None,
            priority=index % 7,
            key=("cancel", index),
        )
        for index in range(queue_size + ROUNDS)
    ]
    cursor = 0
    cancelled = []

    def cancel_next():
        nonlocal cursor
        cancelled.append(backend.cancel(calls[cursor]))
        cursor += 1

    benchmark.pedantic(cancel_next, rounds=ROUNDS, iterations=1)

    assert all(cancelled)
    # SimPyBackend uses cancellation tombstones; calls leave _scheduled only
    # when their queue events are eventually consumed.
    assert len(backend._cancelled) == ROUNDS
    assert len(backend._scheduled) == queue_size + ROUNDS
    benchmark.extra_info["initial_queue_size"] = queue_size + ROUNDS
    benchmark.extra_info["cancelled_calls"] = ROUNDS


@pytest.mark.parametrize("queue_size", QUEUE_SIZES)
@pytest.mark.parametrize("distribution", ["same_timestamp", "spread"])
def test_temporal_backend_step(benchmark, queue_size, distribution):
    backend = SimPyBackend(origin=ORIGIN)
    executed = []

    for index in range(queue_size):
        due_at = (
            ORIGIN + timedelta(seconds=1)
            if distribution == "same_timestamp"
            else ORIGIN + timedelta(seconds=index + 1)
        )
        backend.schedule_at(
            due_at,
            lambda index=index: executed.append(index),
            priority=index % 7,
            key=("step", index),
        )

    benchmark.pedantic(backend.step, rounds=ROUNDS, iterations=1)

    assert len(executed) == ROUNDS
    benchmark.extra_info["initial_queue_size"] = queue_size
    benchmark.extra_info["same_timestamp_density"] = (
        queue_size if distribution == "same_timestamp" else 1
    )
    benchmark.extra_info["priorities"] = 7


@pytest.mark.parametrize("batch_size", RUN_UNTIL_BATCHES)
def test_temporal_backend_run_until_boundary_batch(benchmark, batch_size):
    backend = SimPyBackend(origin=ORIGIN)
    executed = []
    for round_index in range(ROUNDS):
        due_at = ORIGIN + timedelta(seconds=round_index + 1)
        for index in range(batch_size):
            backend.schedule_at(
                due_at,
                lambda round_index=round_index, index=index: executed.append(
                    (round_index, index)
                ),
                priority=index % 7,
                key=("run-until", round_index, index),
            )

    cursor = 0
    processed_per_boundary = []

    def run_next_boundary():
        nonlocal cursor
        target = ORIGIN + timedelta(seconds=cursor + 1)
        processed_per_boundary.append(backend.run_until(target))
        cursor += 1

    benchmark.pedantic(run_next_boundary, rounds=ROUNDS, iterations=1)

    assert processed_per_boundary == [batch_size] * ROUNDS
    assert len(executed) == batch_size * ROUNDS
    assert backend.now == ORIGIN + timedelta(seconds=ROUNDS)
    benchmark.extra_info["events_at_each_boundary"] = batch_size
    benchmark.extra_info["boundaries"] = ROUNDS
    benchmark.extra_info["priority_buckets"] = 7


def _build_engine_with_pending_work(size: int):
    persistence = MemoryPersistence()
    context = SimulationContext(
        clock=SimulationClock(now=ORIGIN, step=timedelta(minutes=1)),
        random=RandomSource(root_seed=1701),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("benchmark", ToggleChart))
    engine = Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        owner_id=OWNER_ID,
    )

    with persistence.transaction(owner_id=OWNER_ID) as uow:
        due_at = ORIGIN + timedelta(hours=1)
        for index in range(size):
            command_id = f"rebuild-command-{index:07d}"
            command = Command(
                command_id=command_id,
                name="flip",
                entity_type="benchmark",
                entity_id=f"entity-{index:07d}",
                due_at=due_at,
                owner_id=OWNER_ID,
            )
            work = ScheduledWork(
                work_id=f"rebuild-work-{index:07d}",
                due_at=due_at,
                priority=index % 7,
                sequence=index + 1,
                command_id=command_id,
                owner_id=OWNER_ID,
            )
            uow.save_command(command)
            uow.save_scheduled_work(work)

    return engine


@pytest.mark.parametrize("size", QUEUE_SIZES)
def test_engine_rebuild_backend_scheduled_work_scaling(benchmark, size):
    engine = _build_engine_with_pending_work(size)
    backends = [SimPyBackend(origin=ORIGIN) for _ in range(ROUNDS)]
    cursor = 0
    rebuilt_counts = []

    def rebuild_next():
        nonlocal cursor
        rebuilt_counts.append(engine.rebuild_backend(backends[cursor]))
        cursor += 1

    benchmark.pedantic(rebuild_next, rounds=ROUNDS, iterations=1)

    assert rebuilt_counts == [size] * ROUNDS
    assert len(backends[-1]._scheduled) == size
    benchmark.extra_info["pending_scheduled_work"] = size
    benchmark.extra_info["rebuilds"] = ROUNDS
    benchmark.extra_info["recovery_participants"] = 4
