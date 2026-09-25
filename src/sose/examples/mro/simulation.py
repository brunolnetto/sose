from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import WorkOrder
from .statecharts import WorkOrderChart


def build_demo() -> tuple[Engine, MemoryPersistence, str]:
    now = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1)),
        random=RandomSource(root_seed=42),
        scheduler=Scheduler(),
    )
    persistence = MemoryPersistence()
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))

    work_order = context.entities.create(
        WorkOrder,
        key=("demo", 1),
        state="planned",
        attributes={"priority": "HIGH"},
    )
    with persistence.transaction() as uow:
        uow.save_entity(work_order)

    engine = Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        rules_for=lambda _: (),
    )

    release = context.commands.create(
        "release",
        target=work_order,
        key=("demo", work_order.id, "release"),
    )
    context.schedules.at(now, command=release)
    return engine, persistence, work_order.id
