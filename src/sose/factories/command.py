from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity

Cause = Command | DomainEvent


class CommandFactory:
    """Creates commands with deterministic IDs and causal metadata."""

    def __init__(self, *, now: Callable[[], datetime], tick: Callable[[], int]) -> None:
        self._now = now
        self._tick = tick
        self._sequence = 0

    def create(
        self,
        name: str,
        *,
        target: Entity,
        due_at: datetime | None = None,
        caused_by: Cause | None = None,
        correlation_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        key: tuple[object, ...] | None = None,
        **payload_fields: Any,
    ) -> Command:
        self._sequence += 1
        issued_at = self._now()
        due = due_at or issued_at
        merged_payload = {**dict(payload or {}), **payload_fields}
        cause_id = _message_id(caused_by)
        correlation = correlation_id or _correlation_id(caused_by)

        identity_parts = key or (
            self._tick(),
            target.entity_type,
            target.id,
            name,
            cause_id or "root",
            self._sequence,
        )
        command_id = deterministic_id("command", *identity_parts)
        return Command(
            command_id=command_id,
            name=name,
            entity_type=target.entity_type,
            entity_id=target.id,
            due_at=due,
            issued_at=issued_at,
            tick=self._tick(),
            payload=merged_payload,
            causation_id=cause_id,
            correlation_id=correlation or command_id,
        )


def _message_id(message: Cause | None) -> str | None:
    if message is None:
        return None
    return getattr(message, "event_id", None) or getattr(message, "command_id", None)


def _correlation_id(message: Cause | None) -> str | None:
    if message is None:
        return None
    return message.correlation_id or _message_id(message)
