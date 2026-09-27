from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class JournalEntry(Entity):
    entity_type: str = "journal_entry"


@dataclass(slots=True)
class ReconciliationItem(Entity):
    entity_type: str = "reconciliation_item"


@dataclass(slots=True)
class Adjustment(Entity):
    entity_type: str = "accounting_adjustment"


@dataclass(slots=True)
class CloseTask(Entity):
    entity_type: str = "close_task"


@dataclass(slots=True)
class AccountingPeriod(Entity):
    entity_type: str = "accounting_period"
