"""Order-to-cash constants, ids and runtime builder (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .scenarios import ORIGIN
from .statecharts import CollectionCaseChart, ReceivableChart, SalesOrderChart


DUE_DELAY = timedelta(hours=2)
OVERDUE_DELAY = timedelta(hours=2)
COLLECTION_FOLLOWUP_DELAY = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class O2CEntities:
    order_id: str


def flow_correlation_id(order_id: str) -> str:
    return deterministic_id("o2c-flow", order_id)


def receivable_id(order_id: str) -> str:
    return deterministic_id(
        "entity",
        "receivable",
        "o2c-reference",
        order_id,
        "receivable",
    )


def collection_case_id(receivable_id_value: str) -> str:
    return deterministic_id(
        "entity",
        "collection_case",
        "o2c-reference",
        receivable_id_value,
        "collection-case",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 336,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("sales_order", SalesOrderChart))
    registry.register(EntityType("receivable", ReceivableChart))
    registry.register(EntityType("collection_case", CollectionCaseChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )
