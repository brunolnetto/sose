"""Manufacturing constants, flow ids and runtime wiring (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .statecharts import OperationChart, ProductionOrderChart

ORIGIN = datetime(2026, 2, 1, 8, tzinfo=timezone.utc)
SKU = "finished-widget-a"
RAW_SKU = "steel-lot-a"
MATERIAL_CAPACITY = 1_000.0
FINISHED_CAPACITY = 1_000.0


def flow_correlation_id() -> str:
    return deterministic_id("manufacturing-flow", "reference", "po-1")


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 84,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("production_order", ProductionOrderChart))
    registry.register(EntityType("manufacturing_operation", OperationChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def _validate_quantity(quantity: float) -> None:
    if quantity <= 0 or quantity > min(MATERIAL_CAPACITY, FINISHED_CAPACITY):
        raise ValueError("quantity must fit material and finished-goods capacity")
