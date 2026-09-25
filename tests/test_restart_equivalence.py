from __future__ import annotations

import heapq

import pytest
from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.durable import DurableScheduler
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.runtime import SimulationPosition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.mro.entities import WorkOrder
from sose.examples.mro.statecharts import WorkOrderChart
from sose.persistence.memory import MemoryPersistence


ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


class ExecutableBackend:
    def __init__(self, *, now: datetime) -> None:
        self._now = now
        self._sequence = 0
        self._queue = []

    @property
    def now(self) -> datetime:
        return self._now

    def schedule_at(self, at, callback, *, priority=100, key=None):
        self._sequence += 1
        heapq.heappush(self._queue, (at, priority, self._sequence, callback))
        return None

    def run_until(self, at: datetime) -> None:
        while self._queue and self._queue[0][0] <= at:
            due_at, _, _, callback = heapq.heappop(self._queue)
            self._now = due_at
            callback()
        self._now = at


def build_engine(
    persistence: MemoryPersistence,
    *,
    now: datetime,
    tick: int = 0,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=42),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))
    return context, Engine(context=context, registry=registry, persistence=persistence)


def seed_workflow(persistence: MemoryPersistence) -> str:
    context, _ = build_engine(persistence, now=ORIGIN)
    work_order = context.entities.create(
        WorkOrder,
        key=("restart-equivalence", 1),
        state="planned",
    )
    with persistence.transaction() as uow:
        uow.save_entity(work_order)

    scheduler = DurableScheduler(persistence)
    for offset, name in (
        (1, "release"),
        (2, "start"),
        (3, "complete"),
        (4, "close"),
    ):
        command = context.commands.create(
            name,
            target=work_order,
            due_at=ORIGIN + timedelta(hours=offset),
            key=("restart-equivalence", work_order.id, name),
        )
        scheduler.schedule(command)

    return work_order.id


def snapshot(persistence: MemoryPersistence, work_order_id: str):
    entity = persistence.entity("work_order", work_order_id)
    position = persistence.simulation_position()
    return {
        "entity": entity,
        "events": persistence.events(),
        "scheduled_work": persistence.scheduled_work(),
        "position": position,
    }


def test_restart_execution_is_equivalent_to_continuous_execution():
    continuous_store = MemoryPersistence()
    continuous_id = seed_workflow(continuous_store)
    _, continuous_engine = build_engine(continuous_store, now=ORIGIN)
    continuous_backend = ExecutableBackend(now=ORIGIN)

    assert continuous_engine.rebuild_backend(continuous_backend) == 4
    continuous_backend.run_until(ORIGIN + timedelta(hours=4))
    continuous = snapshot(continuous_store, continuous_id)

    restarted_store = MemoryPersistence()
    restarted_id = seed_workflow(restarted_store)
    _, first_engine = build_engine(restarted_store, now=ORIGIN)
    first_backend = ExecutableBackend(now=ORIGIN)

    assert first_engine.rebuild_backend(first_backend) == 4
    first_backend.run_until(ORIGIN + timedelta(hours=2))

    position = restarted_store.simulation_position()
    assert position is not None
    assert position.logical_time == ORIGIN + timedelta(hours=2)

    _, second_engine = build_engine(restarted_store, now=position.logical_time)
    second_backend = ExecutableBackend(now=position.logical_time)

    assert second_engine.rebuild_backend(second_backend) == 2
    second_backend.run_until(ORIGIN + timedelta(hours=4))
    restarted = snapshot(restarted_store, restarted_id)

    assert restarted == continuous
    assert restarted["entity"].state == "closed"
    assert restarted["scheduled_work"] == ()
    assert restarted["position"].execution_sequence == 4
    assert restarted["position"].committed_sequence == 4


def test_restart_restores_nonzero_logical_tick():
    continuous_store = MemoryPersistence()
    continuous_id = seed_workflow(continuous_store)
    continuous_context, continuous_engine = build_engine(
        continuous_store,
        now=ORIGIN,
        tick=7,
    )
    continuous_backend = ExecutableBackend(now=ORIGIN)

    assert continuous_engine.rebuild_backend(continuous_backend) == 4
    continuous_backend.run_until(ORIGIN + timedelta(hours=2))

    position = continuous_store.simulation_position()
    assert position is not None
    assert position.logical_tick == 7
    assert continuous_context.clock.tick == 7

    restarted_store = MemoryPersistence()
    restarted_id = seed_workflow(restarted_store)
    _, first_engine = build_engine(restarted_store, now=ORIGIN, tick=7)
    first_backend = ExecutableBackend(now=ORIGIN)

    assert first_engine.rebuild_backend(first_backend) == 4
    first_backend.run_until(ORIGIN + timedelta(hours=1))

    restart_position = restarted_store.simulation_position()
    assert restart_position is not None
    assert restart_position.logical_tick == 7

    restarted_context, second_engine = build_engine(
        restarted_store,
        now=restart_position.logical_time,
        tick=0,
    )
    second_backend = ExecutableBackend(now=restart_position.logical_time)

    assert second_engine.rebuild_backend(second_backend) == 3
    assert restarted_context.clock.tick == 7

    second_backend.run_until(ORIGIN + timedelta(hours=2))

    assert [
        (event.event_id, event.tick)
        for event in restarted_store.events()
    ] == [
        (event.event_id, event.tick)
        for event in continuous_store.events()
    ]
    assert restarted_store.entity("work_order", restarted_id).state == continuous_store.entity(
        "work_order", continuous_id
    ).state


def test_advance_tick_persists_and_restores_next_tick_boundary():
    store = MemoryPersistence()
    context, engine = build_engine(store, now=ORIGIN, tick=3)

    engine.advance_tick()

    position = store.simulation_position()
    assert position is not None
    assert position.logical_time == ORIGIN + timedelta(hours=1)
    assert position.logical_tick == 4
    assert context.clock.now == ORIGIN + timedelta(hours=1)
    assert context.clock.tick == 4

    restarted_context, restarted_engine = build_engine(
        store,
        now=position.logical_time,
        tick=0,
    )
    backend = ExecutableBackend(now=position.logical_time)

    assert restarted_engine.rebuild_backend(backend) == 0
    assert restarted_context.clock.now == position.logical_time
    assert restarted_context.clock.tick == 4


def test_interrupted_tick_rolls_back_durable_items_and_preserves_tick_start():
    store = MemoryPersistence()
    context, engine = build_engine(store, now=ORIGIN, tick=0)
    work_order = context.entities.create(
        WorkOrder,
        key=("interrupted-tick-boundary", 1),
        state="planned",
    )
    with store.transaction() as uow:
        uow.save_entity(work_order)
        uow.set_simulation_position(
            SimulationPosition(
                logical_time=ORIGIN,
                execution_sequence=0,
                committed_sequence=0,
                logical_tick=0,
            )
        )

    scheduler = DurableScheduler(store)
    release = context.commands.create(
        "release",
        target=work_order,
        due_at=ORIGIN + timedelta(minutes=15),
        key=("interrupted-tick-boundary", work_order.id, "release"),
    )
    invalid_complete = context.commands.create(
        "complete",
        target=work_order,
        due_at=ORIGIN + timedelta(minutes=30),
        key=("interrupted-tick-boundary", work_order.id, "complete"),
    )
    scheduler.schedule(release)
    scheduler.schedule(invalid_complete)

    with pytest.raises(Exception):
        engine.advance_tick()

    position = store.simulation_position()
    assert position is not None
    assert position.logical_time == ORIGIN
    assert position.logical_tick == 0
    assert position.execution_sequence == 0
    assert position.committed_sequence == 0
    assert store.entity("work_order", work_order.id).state == "planned"
    assert store.events() == ()
    assert len(store.scheduled_work()) == 2
    assert context.clock.now == ORIGIN
    assert context.clock.tick == 0
