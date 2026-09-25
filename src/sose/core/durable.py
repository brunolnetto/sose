from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.core.runtime import ScheduledWork
from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class DurableScheduledItem:
    work: ScheduledWork
    command: Command


class RebuildBackend(Protocol):
    @property
    def now(self): ...

    def schedule_at(
        self,
        at,
        callback: Callable[[], None],
        *,
        priority: int = 100,
        key: tuple[object, ...] | None = None,
    ): ...


class DurableScheduler:
    """Persist future execution intent independently from any backend queue."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence

    def schedule(self, command: Command, *, priority: int = 100) -> ScheduledWork:
        if self._persistence.command(command.command_id) is not None:
            raise ValueError(f"command already scheduled: {command.command_id}")

        existing = self._persistence.scheduled_work()
        sequence = max((work.sequence for work in existing), default=0) + 1
        work = ScheduledWork(
            work_id=deterministic_id("scheduled-work", command.command_id),
            due_at=command.due_at,
            priority=priority,
            sequence=sequence,
            command_id=command.command_id,
        )

        with self._persistence.transaction() as uow:
            uow.save_command(command)
            uow.save_scheduled_work(work)

        return work

    def pending(self) -> tuple[DurableScheduledItem, ...]:
        items: list[DurableScheduledItem] = []
        for work in self._persistence.scheduled_work():
            command = self._persistence.command(work.command_id)
            if command is None:
                raise RuntimeError(
                    f"scheduled work references missing command: {work.work_id}/{work.command_id}"
                )
            items.append(DurableScheduledItem(work=work, command=command))
        return tuple(items)


class RuntimeRebuilder:
    """Reconstruct ephemeral backend events from durable semantic state."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence

    def rebuild(
        self,
        backend: RebuildBackend,
        *,
        on_due: Callable[[DurableScheduledItem], None],
    ) -> int:
        position = self._persistence.simulation_position()
        if position is not None and backend.now != position.logical_time:
            raise RuntimeError(
                "backend logical time does not match persisted recovery boundary"
            )

        boundary = position.logical_time if position is not None else backend.now
        items = DurableScheduler(self._persistence).pending()
        for item in items:
            if item.work.due_at < boundary:
                raise RuntimeError(
                    f"scheduled work {item.work.work_id} is before recovery boundary"
                )

            backend.schedule_at(
                item.work.due_at,
                lambda item=item: on_due(item),
                priority=item.work.priority,
                key=("durable-work", item.work.work_id),
            )
        return len(items)
