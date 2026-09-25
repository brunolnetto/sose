from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Command:
    """An intention to affect an entity now or in the future."""

    command_id: str
    name: str
    entity_type: str
    entity_id: str
    due_at: datetime
    issued_at: datetime | None = None
    tick: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    causation_id: str | None = None
    correlation_id: str | None = None

    def rescheduled(self, due_at: datetime) -> "Command":
        return replace(self, due_at=due_at)


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Immutable record of something that actually happened."""

    event_id: str
    name: str
    entity_type: str
    entity_id: str
    occurred_at: datetime
    tick: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    causation_id: str | None = None
    correlation_id: str | None = None
