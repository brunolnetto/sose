"""Record-to-report constants, runtime/seed setup and lookups (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import CounterRandomSource
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .identity import adjustment_id
from .entities import (
    AccountingPeriod,
    Adjustment,
    CloseTask,
    JournalEntry,
    ReconciliationItem,
)
from .scenarios import ORIGIN
from .statecharts import (
    AccountingPeriodChart,
    AdjustmentChart,
    CloseTaskChart,
    JournalEntryChart,
    ReconciliationItemChart,
)


CLOSE_DELAY = timedelta(hours=4)
REOPEN_CLOSE_DELAY = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class R2REntities:
    period_id: str
    journal_id: str
    reconciliation_id: str
    close_task_id: str


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 420,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=CounterRandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("journal_entry", JournalEntryChart))
    registry.register(EntityType("reconciliation_item", ReconciliationItemChart))
    registry.register(EntityType("accounting_adjustment", AdjustmentChart))
    registry.register(EntityType("close_task", CloseTaskChart))
    registry.register(EntityType("accounting_period", AccountingPeriodChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    amount: float = 1000.0,
    currency: str = "USD",
    instance_key: str | None = None,
) -> R2REntities:
    if amount <= 0:
        raise ValueError("amount must be positive")
    if not currency:
        raise ValueError("currency must be non-empty")
    if instance_key is not None and (not isinstance(instance_key, str) or not instance_key.strip()):
        raise ValueError("instance_key must be a nonempty string")
    prefix = ("r2r-reference",) if instance_key is None else ("r2r-reference", instance_key)
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
    period = context.entities.create(
        AccountingPeriod,
        key=(*prefix, "period-2026-09"),
        state="open",
        attributes={"period": "2026-09", "reopen_count": 0},
    )
    journal = context.entities.create(
        JournalEntry,
        key=(*prefix, period.id, "journal-1"),
        state="drafted",
        attributes={
            "period_id": period.id,
            "amount": float(amount),
            "currency": currency,
        },
    )
    reconciliation = context.entities.create(
        ReconciliationItem,
        key=(*prefix, period.id, "reconciliation-1"),
        state="pending",
        attributes={
            "period_id": period.id,
            "journal_id": journal.id,
            "amount": float(amount),
            "currency": currency,
        },
    )
    close_task = context.entities.create(
        CloseTask,
        key=(*prefix, period.id, "close-task", 1),
        state="pending",
        attributes={"period_id": period.id, "ordinal": 1},
    )
    with persistence.transaction() as uow:
        for entity in (period, journal, reconciliation, close_task):
            uow.save_entity(entity)
        uow.save_resource_definition(
            ResourceDefinition("posting_processor", capacity=1)
        )
        uow.save_resource_definition(
            ResourceDefinition("reconciliation_analyst", capacity=1)
        )
        uow.save_resource_definition(
            ResourceDefinition("close_accountant", capacity=1)
        )
    return R2REntities(
        period_id=period.id,
        journal_id=journal.id,
        reconciliation_id=reconciliation.id,
        close_task_id=close_task.id,
    )


def _entity(persistence: MemoryPersistence, entity_type: str, entity_id: str):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _period(persistence: MemoryPersistence, entities: R2REntities) -> AccountingPeriod:
    return _entity(persistence, "accounting_period", entities.period_id)


def _journal(persistence: MemoryPersistence, entities: R2REntities) -> JournalEntry:
    return _entity(persistence, "journal_entry", entities.journal_id)


def _reconciliation(
    persistence: MemoryPersistence,
    entities: R2REntities,
) -> ReconciliationItem:
    return _entity(
        persistence,
        "reconciliation_item",
        entities.reconciliation_id,
    )


def _adjustment(
    persistence: MemoryPersistence,
    entities: R2REntities,
) -> Adjustment | None:
    return persistence.entity(
        "accounting_adjustment",
        adjustment_id(entities.reconciliation_id),
    )


def _close_task(
    persistence: MemoryPersistence,
    task_id: str,
) -> CloseTask:
    return _entity(persistence, "close_task", task_id)


def _dispatch(
    engine: Engine,
    entity,
    event: str,
    *,
    key: tuple[object, ...],
    correlation_id: str,
) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)
