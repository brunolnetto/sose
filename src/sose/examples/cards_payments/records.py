"""Cards-payments constants, ids, runtime builder and lookups (split from simulation.py)."""

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

from .entities import Dispute, Payment
from .scenarios import ORIGIN
from .statecharts import DisputeChart, PaymentChart


SETTLEMENT_DELAY = timedelta(hours=2)
RETRY_DELAY = timedelta(hours=2)
EVIDENCE_DELAY = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class PaymentEntities:
    payment_id: str


def flow_correlation_id() -> str:
    return deterministic_id("cards-payment-flow", "reference", "payment-1")


def dispute_entity_id() -> str:
    return deterministic_id(
        "entity",
        "payment_dispute",
        "cards-reference",
        "payment-1",
        "dispute-1",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 168,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("card_payment", PaymentChart))
    registry.register(EntityType("payment_dispute", DisputeChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )

def _payment(persistence: MemoryPersistence, entities: PaymentEntities) -> Payment:
    payment = persistence.entity("card_payment", entities.payment_id)
    if payment is None:
        raise RuntimeError("payment was not persisted")
    return payment


def _dispute(persistence: MemoryPersistence) -> Dispute | None:
    return persistence.entity("payment_dispute", dispute_entity_id())

def _processor_available(engine: Engine) -> bool:
    return bool(
        engine.context.scenarios.attribute("payments.processor.available", True)
    )
