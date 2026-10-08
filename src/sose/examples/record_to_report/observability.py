from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sose.persistence.base import Persistence

from .identity import adjustment_id, close_task_id

if TYPE_CHECKING:
    from .simulation import R2REntities


@dataclass(frozen=True, slots=True)
class RecordToReportProjection:
    """Read-only canonical projection of durable Record-to-Report truth."""

    period_id: str
    journal_id: str
    reconciliation_id: str
    adjustment_id: str | None
    close_task_id: str
    period_state: str
    journal_state: str
    reconciliation_state: str
    adjustment_state: str | None
    close_task_state: str
    amount: float
    currency: str
    closed: bool
    close_cycle_ordinal: int
    close_cycle_seconds: float | None


@dataclass(frozen=True, slots=True)
class RecordToReportKpis:
    """Canonical R2R KPIs derived from durable state and immutable events."""

    close_cycle_seconds: float | None
    amount: float
    transition_count: int
    rejected_posting_count: int
    unmatched_count: int
    adjustment_count: int
    adjustment_posted_count: int
    close_count: int
    reopen_count: int
    close_task_completed_count: int
    closed: bool


def _required_entity(
    persistence: Persistence,
    entity_type: str,
    entity_id: str,
):
    entity = persistence.entity(entity_type, entity_id)
    if entity is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return entity


def _close_cycle_ordinal(period) -> int:
    return int(period.attributes.get("reopen_count", 0)) + 1


def _process_entity_ids(
    *,
    period_id: str,
    journal_id: str,
    reconciliation_id: str,
    close_cycle_ordinal: int,
) -> set[str]:
    ids = {
        period_id,
        journal_id,
        reconciliation_id,
        adjustment_id(reconciliation_id),
    }
    ids.update(
        close_task_id(period_id, ordinal)
        for ordinal in range(1, close_cycle_ordinal + 1)
    )
    return ids


def _completion_event(
    persistence: Persistence,
    *,
    task_id: str,
):
    events = tuple(
        event
        for event in persistence.events()
        if event.name == "entity.state_transition"
        and event.entity_type == "close_task"
        and event.entity_id == task_id
        and event.payload.get("to_state") == "completed"
    )
    return min(events, key=lambda event: event.occurred_at) if events else None


def record_to_report_projection(
    persistence: Persistence,
    *,
    entities: R2REntities,
) -> RecordToReportProjection:
    """Project current R2R durable truth without mutating process state."""

    period = _required_entity(persistence, "accounting_period", entities.period_id)
    journal = _required_entity(persistence, "journal_entry", entities.journal_id)
    reconciliation = _required_entity(
        persistence,
        "reconciliation_item",
        entities.reconciliation_id,
    )

    adjustment_key = adjustment_id(reconciliation.id)
    adjustment = persistence.entity("accounting_adjustment", adjustment_key)

    ordinal = _close_cycle_ordinal(period)
    task_key = close_task_id(period.id, ordinal)
    task = _required_entity(persistence, "close_task", task_key)
    completion = _completion_event(persistence, task_id=task.id)

    close_cycle_seconds = None
    if task.created_at is not None and completion is not None:
        close_cycle_seconds = max(
            0.0,
            (completion.occurred_at - task.created_at).total_seconds(),
        )

    return RecordToReportProjection(
        period_id=period.id,
        journal_id=journal.id,
        reconciliation_id=reconciliation.id,
        adjustment_id=adjustment.id if adjustment is not None else None,
        close_task_id=task.id,
        period_state=period.state,
        journal_state=journal.state,
        reconciliation_state=reconciliation.state,
        adjustment_state=adjustment.state if adjustment is not None else None,
        close_task_state=task.state,
        amount=float(journal.attributes["amount"]),
        currency=str(journal.attributes["currency"]),
        closed=period.state == "closed",
        close_cycle_ordinal=ordinal,
        close_cycle_seconds=close_cycle_seconds,
    )


def record_to_report_kpis(
    persistence: Persistence,
    *,
    entities: R2REntities,
) -> RecordToReportKpis:
    """Return stable R2R KPIs from projection and immutable event history."""

    projection = record_to_report_projection(persistence, entities=entities)
    process_ids = _process_entity_ids(
        period_id=projection.period_id,
        journal_id=projection.journal_id,
        reconciliation_id=projection.reconciliation_id,
        close_cycle_ordinal=projection.close_cycle_ordinal,
    )
    events = tuple(
        event
        for event in persistence.events()
        if event.name == "entity.state_transition"
        and event.entity_id in process_ids
    )

    def count(entity_type: str, to_state: str) -> int:
        return sum(
            event.entity_type == entity_type
            and event.payload.get("to_state") == to_state
            for event in events
        )

    return RecordToReportKpis(
        close_cycle_seconds=projection.close_cycle_seconds,
        amount=projection.amount,
        transition_count=len(events),
        rejected_posting_count=count("journal_entry", "rejected"),
        unmatched_count=count("reconciliation_item", "unmatched"),
        adjustment_count=int(projection.adjustment_id is not None),
        adjustment_posted_count=count("accounting_adjustment", "posted"),
        close_count=count("accounting_period", "closed"),
        reopen_count=count("accounting_period", "reopened"),
        close_task_completed_count=count("close_task", "completed"),
        closed=projection.closed,
    )
