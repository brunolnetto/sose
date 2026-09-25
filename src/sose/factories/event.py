from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity

Cause = Command | DomainEvent


class EventFactory:
    """Canonical constructor for immutable SOSE domain events."""

    def __init__(self, *, now: Callable[[], datetime], tick: Callable[[], int]) -> None:
        self._now = now
        self._tick = tick
        self._sequence = 0

    def create(
        self,
        name: str,
        *,
        entity: Entity,
        caused_by: Cause | None = None,
        correlation_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        key: tuple[object, ...] | None = None,
        **payload_fields: Any,
    ) -> DomainEvent:
        self._sequence += 1
        occurred_at = self._now()
        merged_payload = {**dict(payload or {}), **payload_fields}
        cause_id = _message_id(caused_by)
        correlation = correlation_id or _correlation_id(caused_by)

        identity_parts = key or (
            self._tick(),
            entity.entity_type,
            entity.id,
            name,
            cause_id or "root",
            self._sequence,
        )
        event_id = deterministic_id("event", *identity_parts)
        return DomainEvent(
            event_id=event_id,
            name=name,
            entity_type=entity.entity_type,
            entity_id=entity.id,
            occurred_at=occurred_at,
            tick=self._tick(),
            payload=merged_payload,
            causation_id=cause_id,
            correlation_id=correlation or event_id,
        )

    def transition(
        self,
        *,
        entity: Entity,
        trigger: str,
        from_state: str,
        to_state: str,
        caused_by: Cause | None = None,
    ) -> DomainEvent:
        return self.create(
            "entity.state_transition",
            entity=entity,
            caused_by=caused_by,
            key=(
                self._tick(),
                entity.entity_type,
                entity.id,
                entity.version,
                trigger,
                from_state,
                to_state,
            ),
            trigger=trigger,
            from_state=from_state,
            to_state=to_state,
        )

    def exception(
        self,
        name: str,
        *,
        entity: Entity,
        caused_by: Cause | None = None,
        **payload: Any,
    ) -> DomainEvent:
        return self.create(
            f"exception.{name}",
            entity=entity,
            caused_by=caused_by,
            **payload,
        )


def _message_id(message: Cause | None) -> str | None:
    if message is None:
        return None
    return getattr(message, "event_id", None) or getattr(message, "command_id", None)


def _correlation_id(message: Cause | None) -> str | None:
    if message is None:
        return None
    return message.correlation_id or _message_id(message)
