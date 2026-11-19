"""Logistics constants, ids, runtime builder and entity lookups (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import DeliveryAttempt, Shipment
from .statecharts import DeliveryAttemptChart, ShipmentChart


ORIGIN = datetime(2026, 3, 1, 8, tzinfo=timezone.utc)
PICKUP_DUE = ORIGIN + timedelta(hours=1)
RETRY_DELAY = timedelta(hours=2)


def _organization_queue(owner_id: str, queue: str) -> str:
    """Physical Store names are owner-qualified; independent FIFO heads cannot alias."""
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("store queue owner_id must not be empty")
    if queue not in {"origin_hub_queue", "destination_hub_queue"}:
        raise ValueError("unknown Logistics queue")
    return f"{owner_id}/{queue}"


@dataclass(frozen=True, slots=True)
class LogisticsEntities:
    shipment_id: str


def flow_correlation_id() -> str:
    return deterministic_id("logistics-flow", "reference", "shipment-1")


def delivery_attempt_id(ordinal: int, *, shipment_id: str | None = None) -> str:
    if ordinal < 1:
        raise ValueError("delivery attempt ordinal must be >= 1")
    reference_shipment = deterministic_id("entity", "shipment", "logistics-reference", "shipment-1")
    if shipment_id is not None and not shipment_id:
        raise ValueError("shipment_id must be nonempty")
    identity = "shipment-1" if shipment_id in (None, reference_shipment) else shipment_id
    return deterministic_id(
        "entity", "delivery_attempt", "logistics-reference", identity, "attempt", ordinal,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 126,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("shipment", ShipmentChart))
    registry.register(EntityType("delivery_attempt", DeliveryAttemptChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )

def _shipment(persistence: MemoryPersistence, entities: LogisticsEntities) -> Shipment:
    shipment = persistence.entity("shipment", entities.shipment_id)
    if shipment is None:
        raise RuntimeError("shipment was not persisted")
    return shipment


def _attempt(
    persistence: MemoryPersistence, ordinal: int, *, shipment_id: str | None = None,
) -> DeliveryAttempt | None:
    return persistence.entity(
        "delivery_attempt", delivery_attempt_id(ordinal, shipment_id=shipment_id),
    )
