"""Batch-size benchmarks for Engine.advance_tick()."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os

import pytest
from statemachine import State, StateChart

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
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
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=1)
OWNER_ID = "benchmark-engine-tick"


class ToggleChart(StateChart):
    idle = State(initial=True)
    busy = State()

    flip = idle.to(busy) | busy.to(idle)


BATCH_CASES = (
    ((0, 1), (1, 1), (10, 1), (10, 5))
    if QUICK
    else (
        (0, 1),
        (1, 1),
        (10, 1),
        (10, 5),
        (100, 1),
        (100, 5),
        (1_000, 1),
        (1_000, 5),
    )
)


def _build_tick_engine(batch_size: int, commands_per_entity: int):
    distinct_entities = batch_size // commands_per_entity
    persistence = MemoryPersistence()
    context = SimulationContext(
        clock=SimulationClock(now=ORIGIN, step=STEP),
        random=RandomSource(root_seed=1701),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("benchmark", ToggleChart))
    entities = [
        Entity(
            id=f"entity-{index:06d}",
            entity_type="benchmark",
            state="idle",
            attributes={"ordinal": index},
        )
        for index in range(distinct_entities)
    ]
    engine = Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        owner_id=OWNER_ID,
    )

    # Seed entities and the complete future schedule in one transaction. The
    # benchmark measures advance_tick(), not DurableScheduler.schedule() setup.
    with persistence.transaction(owner_id=OWNER_ID) as uow:
        for entity in entities:
            uow.save_entity(entity)

        sequence = 0
        for tick_index in range(ROUNDS):
            due_at = ORIGIN + STEP * (tick_index + 1)
            for command_index in range(batch_size):
                entity_index = command_index // commands_per_entity
                entity = entities[entity_index]
                sequence += 1
                command_id = f"tick-command-{tick_index:02d}-{command_index:06d}"
                command = Command(
                    command_id=command_id,
                    name="flip",
                    entity_type=entity.entity_type,
                    entity_id=entity.id,
                    due_at=due_at,
                    owner_id=OWNER_ID,
                )
                work = ScheduledWork(
                    work_id=f"tick-work-{tick_index:02d}-{command_index:06d}",
                    due_at=due_at,
                    priority=100,
                    sequence=sequence,
                    command_id=command_id,
                    owner_id=OWNER_ID,
                )
                uow.save_command(command)
                uow.save_scheduled_work(work)

    return persistence, context, engine, entities, distinct_entities


@pytest.mark.parametrize("batch_size,commands_per_entity", BATCH_CASES)
def test_engine_advance_tick_due_batch_scaling(
    benchmark,
    batch_size,
    commands_per_entity,
):
    (
        persistence,
        context,
        engine,
        entities,
        distinct_entities,
    ) = _build_tick_engine(batch_size, commands_per_entity)

    benchmark.pedantic(
        engine.advance_tick,
        rounds=ROUNDS,
        iterations=1,
    )

    assert persistence.scheduled_work() == ()
    assert len(persistence.events()) == batch_size * ROUNDS
    assert context.clock.tick == ROUNDS

    final_state = "busy" if (ROUNDS * commands_per_entity) % 2 else "idle"
    for entity in entities:
        stored = persistence.entity("benchmark", entity.id)
        assert stored is not None
        assert stored.state == final_state

    benchmark.extra_info["due_commands_per_tick"] = batch_size
    benchmark.extra_info["distinct_entities"] = distinct_entities
    benchmark.extra_info["commands_per_entity"] = commands_per_entity
    benchmark.extra_info["future_backlog_after_first_tick"] = batch_size * (ROUNDS - 1)
    benchmark.extra_info["ticks"] = ROUNDS
    benchmark.extra_info["events_emitted"] = batch_size * ROUNDS
