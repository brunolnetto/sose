from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

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


def flow_correlation_id(period_id: str) -> str:
    return deterministic_id("r2r-flow", period_id)


def adjustment_id(reconciliation_id: str) -> str:
    return deterministic_id(
        "entity",
        "accounting_adjustment",
        "r2r-reference",
        reconciliation_id,
        "adjustment-1",
    )


def close_task_id(period_id: str, ordinal: int) -> str:
    return deterministic_id(
        "entity",
        "close_task",
        "r2r-reference",
        period_id,
        "close-task",
        ordinal,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 420,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
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
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    amount: float = 1000.0,
    currency: str = "USD",
) -> R2REntities:
    if amount <= 0:
        raise ValueError("amount must be positive")
    if not currency:
        raise ValueError("currency must be non-empty")
    context, _ = build_runtime(persistence)
    period = context.entities.create(
        AccountingPeriod,
        key=("r2r-reference", "period-2026-09"),
        state="open",
        attributes={"period": "2026-09", "reopen_count": 0},
    )
    journal = context.entities.create(
        JournalEntry,
        key=("r2r-reference", period.id, "journal-1"),
        state="drafted",
        attributes={
            "period_id": period.id,
            "amount": float(amount),
            "currency": currency,
        },
    )
    reconciliation = context.entities.create(
        ReconciliationItem,
        key=("r2r-reference", period.id, "reconciliation-1"),
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
        key=("r2r-reference", period.id, "close-task", 1),
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


def submit_and_post_journal(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: R2REntities,
    reject: bool = False,
) -> bool:
    journal = _journal(persistence, entities)
    request_id = f"posting:{journal.id}"
    if journal.state in {"posted", "rejected"}:
        engine.resources.withdraw(backend, request_id)
        return journal.state == "posted"

    available = bool(
        engine.context.scenarios.attribute("r2r.posting.available", True)
    )
    if not available:
        engine.resources.withdraw(backend, request_id)
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="posting_processor",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(entities.period_id)
    journal = _journal(persistence, entities)
    if journal.state == "drafted":
        _dispatch(
            engine,
            journal,
            "submit",
            key=("r2r", journal.id, "submit"),
            correlation_id=correlation_id,
        )
        journal = _journal(persistence, entities)

    if journal.state == "submitted":
        _dispatch(
            engine,
            journal,
            "reject" if reject else "post",
            key=("r2r", journal.id, "reject" if reject else "post"),
            correlation_id=correlation_id,
        )

    engine.resources.withdraw(backend, request_id)
    return _journal(persistence, entities).state == "posted"


def reconcile_item(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: R2REntities,
    outcome: str = "match",
) -> bool:
    if outcome not in {"match", "unmatched"}:
        raise ValueError(f"unsupported reconciliation outcome: {outcome}")

    journal = _journal(persistence, entities)
    if journal.state != "posted":
        raise RuntimeError("reconciliation requires durable posted journal evidence")

    item = _reconciliation(persistence, entities)
    request_id = f"reconciliation-analyst:{item.id}"
    if item.state in {"matched", "reconciled", "rejected"}:
        engine.resources.withdraw(backend, request_id)
        return item.state in {"matched", "reconciled"}

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="reconciliation_analyst",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(entities.period_id)
    item = _reconciliation(persistence, entities)
    if item.state == "pending":
        _dispatch(
            engine,
            item,
            "start",
            key=("r2r", item.id, "start"),
            correlation_id=correlation_id,
        )
        item = _reconciliation(persistence, entities)

    if item.state == "reconciling":
        event = "match" if outcome == "match" else "mark_unmatched"
        _dispatch(
            engine,
            item,
            event,
            key=("r2r", item.id, event),
            correlation_id=correlation_id,
        )

    engine.resources.withdraw(backend, request_id)
    return _reconciliation(persistence, entities).state == "matched"


def ensure_adjustment(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: R2REntities,
) -> Adjustment:
    existing = _adjustment(persistence, entities)
    if existing is not None:
        return existing

    item = _reconciliation(persistence, entities)
    if item.state == "unmatched":
        _dispatch(
            engine,
            item,
            "require_adjustment",
            key=("r2r", item.id, "require-adjustment"),
            correlation_id=flow_correlation_id(entities.period_id),
        )
        item = _reconciliation(persistence, entities)

    if item.state != "adjustment_required":
        raise RuntimeError(
            "adjustment requires ReconciliationItem(adjustment_required)"
        )

    adjustment = engine.context.entities.create(
        Adjustment,
        key=("r2r-reference", item.id, "adjustment-1"),
        state="proposed",
        attributes={
            "period_id": entities.period_id,
            "reconciliation_id": item.id,
            "amount": float(item.attributes["amount"]),
            "currency": item.attributes["currency"],
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(adjustment)
    return adjustment


def post_adjustment(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: R2REntities,
    reject: bool = False,
) -> bool:
    adjustment = ensure_adjustment(
        persistence,
        engine,
        entities=entities,
    )
    request_id = f"posting-adjustment:{adjustment.id}"
    if adjustment.state in {"posted", "rejected"}:
        engine.resources.withdraw(backend, request_id)
        if adjustment.state == "posted":
            item = _reconciliation(persistence, entities)
            if item.state == "adjustment_required":
                _dispatch(
                    engine,
                    item,
                    "apply_adjustment",
                    key=("r2r", item.id, adjustment.id, "apply-adjustment"),
                    correlation_id=flow_correlation_id(entities.period_id),
                )
            return True
        return False

    available = bool(
        engine.context.scenarios.attribute("r2r.posting.available", True)
    )
    if not available:
        engine.resources.withdraw(backend, request_id)
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="posting_processor",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(entities.period_id)
    adjustment = _adjustment(persistence, entities)
    if adjustment is None:
        raise RuntimeError("adjustment disappeared")

    if adjustment.state == "proposed":
        _dispatch(
            engine,
            adjustment,
            "submit",
            key=("r2r-adjustment", adjustment.id, "submit"),
            correlation_id=correlation_id,
        )
        adjustment = _adjustment(persistence, entities)

    if adjustment is not None and adjustment.state == "submitted":
        _dispatch(
            engine,
            adjustment,
            "approve",
            key=("r2r-adjustment", adjustment.id, "approve"),
            correlation_id=correlation_id,
        )
        adjustment = _adjustment(persistence, entities)

    if adjustment is not None and adjustment.state == "approved":
        _dispatch(
            engine,
            adjustment,
            "reject" if reject else "post",
            key=(
                "r2r-adjustment",
                adjustment.id,
                "reject" if reject else "post",
            ),
            correlation_id=correlation_id,
        )

    engine.resources.withdraw(backend, request_id)

    adjustment = _adjustment(persistence, entities)
    if adjustment is None or adjustment.state != "posted":
        return False

    item = _reconciliation(persistence, entities)
    if item.state == "adjustment_required":
        _dispatch(
            engine,
            item,
            "apply_adjustment",
            key=("r2r", item.id, adjustment.id, "apply-adjustment"),
            correlation_id=correlation_id,
        )
    return True


def _scheduled_close_work(
    persistence: MemoryPersistence,
    task_id: str,
):
    for work in persistence.scheduled_work():
        command = persistence.command(work.command_id)
        if (
            command is not None
            and command.entity_type == "close_task"
            and command.entity_id == task_id
            and command.name == "start"
        ):
            return work, command
    return None


def schedule_close(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    task_id: str,
    period_id: str,
    delay: timedelta = CLOSE_DELAY,
):
    task = _close_task(persistence, task_id)
    if task.state == "in_progress":
        return backend.now
    if task.state != "pending":
        raise RuntimeError(f"close task cannot be scheduled from {task.state}")

    existing = _scheduled_close_work(persistence, task.id)
    if existing is not None:
        return existing[0].due_at

    due_at = backend.now + delay
    command = engine.context.commands.create(
        "start",
        target=task,
        due_at=due_at,
        correlation_id=flow_correlation_id(period_id),
        key=("r2r-close", task.id, "start"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def _period_ready_for_close(
    persistence: MemoryPersistence,
    entities: R2REntities,
) -> bool:
    journal = _journal(persistence, entities)
    item = _reconciliation(persistence, entities)
    return journal.state == "posted" and item.state in {"matched", "reconciled"}


def reconcile_close(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: R2REntities,
    task_id: str | None = None,
) -> bool:
    task_id = task_id or entities.close_task_id
    task = _close_task(persistence, task_id)
    period = _period(persistence, entities)

    request_id = f"close-accountant:{task.id}"
    if period.state == "closed" and task.state == "completed":
        engine.resources.withdraw(backend, request_id)
        return True
    if task.state != "in_progress":
        return False
    if not _period_ready_for_close(persistence, entities):
        return False

    available = bool(
        engine.context.scenarios.attribute("r2r.close_team.available", True)
    )
    if not available:
        engine.resources.withdraw(backend, request_id)
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="close_accountant",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(period.id)
    period = _period(persistence, entities)
    if period.state in {"open", "reopened"}:
        _dispatch(
            engine,
            period,
            "prepare_close",
            key=("r2r-period", period.id, "prepare-close", period.version),
            correlation_id=correlation_id,
        )
        period = _period(persistence, entities)

    if period.state == "close_ready":
        _dispatch(
            engine,
            period,
            "close",
            key=("r2r-period", period.id, "close", period.version),
            correlation_id=correlation_id,
        )

    task = _close_task(persistence, task.id)
    if task.state == "in_progress":
        _dispatch(
            engine,
            task,
            "complete",
            key=("r2r-close", task.id, "complete"),
            correlation_id=correlation_id,
        )

    engine.resources.withdraw(backend, request_id)
    return _period(persistence, entities).state == "closed"


def reopen_period(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: R2REntities,
) -> CloseTask:
    period = _period(persistence, entities)
    if period.state != "closed":
        raise RuntimeError("only a closed period can be reopened")

    correlation_id = flow_correlation_id(period.id)
    _dispatch(
        engine,
        period,
        "reopen",
        key=("r2r-period", period.id, "reopen", period.version),
        correlation_id=correlation_id,
    )
    period = _period(persistence, entities)

    ordinal = int(period.attributes.get("reopen_count", 0)) + 2
    period.attributes["reopen_count"] = ordinal - 1
    task = engine.context.entities.create(
        CloseTask,
        key=("r2r-reference", period.id, "close-task", ordinal),
        state="pending",
        attributes={"period_id": period.id, "ordinal": ordinal},
    )
    with persistence.transaction() as uow:
        uow.save_entity(period)
        uow.save_entity(task)
    return task


def run_happy_path() -> tuple[MemoryPersistence, R2REntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    if not submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("journal posting failed")
    if not reconcile_item(
        persistence,
        engine,
        backend,
        entities=entities,
        outcome="match",
    ):
        raise RuntimeError("reconciliation failed")

    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
    )
    backend.run_until(due_at)
    if not reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("period close failed")
    return persistence, entities


def run_adjustment_path() -> tuple[MemoryPersistence, R2REntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    if not submit_and_post_journal(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("journal posting failed")
    reconcile_item(
        persistence,
        engine,
        backend,
        entities=entities,
        outcome="unmatched",
    )
    if not post_adjustment(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("adjustment posting failed")

    due_at = schedule_close(
        persistence,
        engine,
        backend,
        task_id=entities.close_task_id,
        period_id=entities.period_id,
    )
    backend.run_until(due_at)
    if not reconcile_close(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("period close failed")
    return persistence, entities
