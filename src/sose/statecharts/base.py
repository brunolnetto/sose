from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from statemachine import StateChart

from sose.core.events import Command, DomainEvent
from sose.domain.entity import Entity
from sose.factories.event import EventFactory

Emit = Callable[[DomainEvent], None]
Cause = Command | DomainEvent


@dataclass(slots=True)
class TransitionRecorder:
    """Listener that converts state transitions into canonical SOSE events."""

    entity: Entity
    now: Callable[[], datetime]
    emit: Emit
    events: EventFactory
    caused_by: Cause | None = None

    def after_transition(self, event, source, target, **kwargs) -> None:  # library DI
        at = self.now()
        self.entity.touch(at)
        self.emit(
            self.events.transition(
                entity=self.entity,
                trigger=str(event),
                from_state=source.id,
                to_state=target.id,
                caused_by=self.caused_by,
            )
        )


def attach_recorder(chart: StateChart, recorder: TransitionRecorder) -> StateChart:
    chart.add_listener(recorder)
    return chart
