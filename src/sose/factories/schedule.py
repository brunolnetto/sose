from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from sose.core.events import Command
from sose.core.scheduler import Scheduler


class ScheduleFactory:
    """Domain-facing façade for deterministic logical-time scheduling."""

    def __init__(self, *, now: Callable[[], datetime], scheduler: Scheduler) -> None:
        self._now = now
        self._scheduler = scheduler

    def bind_scheduler(self, scheduler) -> None:
        """Replace the scheduling mechanism while preserving the domain-facing API."""
        self._scheduler = scheduler

    def at(self, at: datetime, *, command: Command, priority: int = 100) -> Command:
        if command.due_at != at:
            command = command.rescheduled(at)
        self._scheduler.schedule(command, priority=priority)
        return command

    def after(
        self,
        delay: timedelta | None = None,
        *,
        command: Command,
        priority: int = 100,
        hours: float = 0,
        minutes: float = 0,
        seconds: float = 0,
    ) -> Command:
        delta = delay or timedelta(hours=hours, minutes=minutes, seconds=seconds)
        if delta < timedelta(0):
            raise ValueError("delay cannot be negative")
        return self.at(self._now() + delta, command=command, priority=priority)
