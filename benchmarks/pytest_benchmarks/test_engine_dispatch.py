"""Component benchmarks for direct Engine.dispatch()."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest
from statemachine import State, StateChart

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.durable import DurableScheduledItem
from sose.core.engine import Engine
from sose.core.events import Command
from sose.core.randomness import RandomSource
from sose.core.runtime import ScheduledWork
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence


QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
ROUNDS = 1 if QUICK else 5
SIZES = [10, 100] if QUICK else [100, 1_000, 10_000]
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNER_ID = "benchmark-engine-dispatch"


class ToggleChart(StateChart):
    """Minimal two-state chart with a reversible event."""

    idle = State(initial=True)
    busy = State()

    flip = idle.to(busy) | busy.to(idle)


class WorkflowChart(StateChart):
    """Small multi-stage chart used to vary topology and transition lookup."""

    planned = State(initial=True)
    queued = State()
    running = State()
    paused = State()
    completed = State()

    enqueue = planned.to(queued)
    start = queued.to(running)
    pause = running.to(paused)
    resume = paused.to(running)
    finish = running.to(completed)


CHART_CASES = (
    pytest.param(
        ToggleChart,
        "idle",
        ("flip", "flip", "flip", "flip", "flip"),
        ("busy", "idle", "busy", "idle", "busy"),
        id="two-state",
    ),
    pytest.param(
        WorkflowChart,
        "planned",
        ("enqueue", "start", "pause", "resume", "finish"),
        ("queued", "running", "paused", "running", "completed"),
        id="five-state",
    ),
)


def _build_engine(entity_count: int, chart_factory, initial_state: str):
    persistence = MemoryPersistence()
    context = SimulationContext(
        clock=SimulationClock(now=ORIGIN, step=timedelta(minutes=1)),
        random=RandomSource(root_seed=1701),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("benchmark", chart_factory))

    entities = [
        Entity(
            id=f"entity-{index:06d}",
            entity_type="benchmark",
            state=initial_state,
            attributes={"ordinal": index},
        )
        for index in range(entity_count)
    ]
    with persistence.transaction(owner_id=OWNER_ID) as uow:
        for entity in entities:
            uow.save_entity(entity)

    engine = Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        owner_id=OWNER_ID,
    )
    return persistence, context, engine, entities


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(
    "chart_factory,initial_state,event_names,expected_states",
    CHART_CASES,
)
def test_engine_dispatch_scaling(
    benchmark,
    size,
    chart_factory,
    initial_state,
    event_names,
    expected_states,
):
    persistence, context, engine, entities = _build_engine(
        size,
        chart_factory,
        initial_state,
    )
    target = entities[0]
    commands = [
        context.commands.create(
            event_name,
            target=target,
            key=("dispatch-benchmark", chart_factory.__name__, index),
        )
        for index, event_name in enumerate(event_names[:ROUNDS])
    ]
    cursor = 0

    def dispatch_next():
        nonlocal cursor
        engine.dispatch(commands[cursor])
        cursor += 1

    benchmark.pedantic(dispatch_next, rounds=ROUNDS, iterations=1)

    stored = persistence.entity("benchmark", target.id)
    assert stored is not None
    assert stored.state == expected_states[ROUNDS - 1]
    assert len(persistence.events()) == ROUNDS

    benchmark.extra_info["total_entities"] = size
    benchmark.extra_info["chart_states"] = (
        5 if chart_factory is WorkflowChart else 2
    )
    benchmark.extra_info["dispatches"] = ROUNDS
    benchmark.extra_info["events_emitted"] = ROUNDS
    benchmark.extra_info["persistence_adapter"] = "memory"


def test_engine_dispatch_scheduled_stale_callback(benchmark):
    """Measure the idempotent no-op path after durable work was consumed."""

    persistence, _, engine, _ = _build_engine(1, ToggleChart, "idle")
    command = Command(
        command_id="already-consumed-command",
        name="flip",
        entity_type="benchmark",
        entity_id="entity-000000",
        due_at=ORIGIN,
        owner_id=OWNER_ID,
    )
    work = ScheduledWork(
        work_id="already-consumed-work",
        due_at=ORIGIN,
        priority=100,
        sequence=1,
        command_id=command.command_id,
        owner_id=OWNER_ID,
    )
    item = DurableScheduledItem(work=work, command=command)
    results = []

    def dispatch_stale():
        results.append(engine.dispatch_scheduled(item))

    benchmark.pedantic(dispatch_stale, rounds=ROUNDS, iterations=1)

    assert results == [False] * ROUNDS
    assert persistence.events() == ()
    assert persistence.scheduled_work() == ()
    benchmark.extra_info["stale_callbacks"] = ROUNDS
