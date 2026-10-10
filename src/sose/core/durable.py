from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import sqlite3
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
        self._backend: RebuildBackend | None = None
        self._on_due: Callable[[DurableScheduledItem], None] | None = None

    def attach_backend(
        self,
        backend: RebuildBackend,
        *,
        on_due: Callable[[DurableScheduledItem], None],
    ) -> None:
        """Attach the active ephemeral backend for schedules created after recovery."""
        self._backend = backend
        self._on_due = on_due

    def detach_backend(self) -> None:
        self._backend = None
        self._on_due = None

    def schedule(self, command: Command, *, priority: int = 100) -> ScheduledWork:
        if self._persistence.command(command.command_id) is not None:
            raise ValueError(f"command already scheduled: {command.command_id}")
        if self._backend is not None and command.due_at < self._backend.now:
            raise ValueError("scheduled work cannot be created before backend logical time")

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

        if self._backend is not None:
            if self._on_due is None:  # pragma: no cover - attachment invariant
                raise RuntimeError("durable scheduler backend is missing on_due callback")
            self._backend.schedule_at(
                work.due_at,
                lambda item=DurableScheduledItem(work=work, command=command): self._on_due(item),
                priority=work.priority,
                key=("durable-work", work.work_id),
            )

        return work

    def find_pending(
        self,
        *,
        entity_type: str,
        entity_id: str,
        name: str,
    ) -> DurableScheduledItem | None:
        """Return the unique pending work matching one target command.

        A missing match means the work is already absent or consumed. Multiple
        matches are rejected because callers that expect one lifecycle should not
        silently pick an arbitrary duplicate.
        """
        matches = tuple(
            item
            for item in self.pending()
            if item.command.entity_type == entity_type
            and item.command.entity_id == entity_id
            and item.command.name == name
        )
        if len(matches) > 1:
            raise RuntimeError(
                "multiple pending scheduled commands match "
                f"{entity_type}/{entity_id}:{name}"
            )
        return None if not matches else matches[0]

    def cancel(self, work_id: str) -> bool:
        """Atomically cancel work plus its Command from the same durable snapshot.

        A competing worker may have already consumed both records. That is an
        ordinary idempotent miss, not an orphaned-work integrity violation.
        A work row *present* without its Command in the same transaction remains
        an error, and must not be silently repaired.
        """
        with self._persistence.transaction() as uow:
            work = uow.get_scheduled_work(work_id)
            if work is None:
                return False
            command = uow.get_command(work.command_id)
            if command is None:
                raise RuntimeError(
                    f"scheduled work references missing command: {work.work_id}/{work.command_id}"
                )
            uow.delete_scheduled_work(work.work_id)
            uow.delete_command(command.command_id)
        return True

    def cancel_pending(
        self,
        *,
        entity_type: str,
        entity_id: str,
        name: str,
    ) -> bool:
        """Cancel the unique pending command matching one semantic lifecycle."""
        item = self.find_pending(
            entity_type=entity_type,
            entity_id=entity_id,
            name=name,
        )
        if item is None:
            return False
        return self.cancel(item.work.work_id)

    def due(self, at) -> tuple[DurableScheduledItem, ...]:
        return tuple(item for item in self.pending() if item.work.due_at <= at)

    def pending(self) -> tuple[DurableScheduledItem, ...]:
        # SQLite adapters use BEGIN IMMEDIATE and cannot open a second
        # transaction inside the first. Existing callers can rebuild a backend
        # while a SQLite transaction is active. Its connection is already the
        # consistent snapshot, so reuse that read scope without starting BEGIN.
        connection = getattr(self._persistence, "_connection", None)
        if isinstance(connection, sqlite3.Connection) and connection.in_transaction:
            items: list[DurableScheduledItem] = []
            for work in self._persistence.scheduled_work():
                command = self._persistence.command(work.command_id)
                if command is None:
                    raise RuntimeError(
                        f"scheduled work references missing command: {work.work_id}/{work.command_id}"
                    )
                items.append(DurableScheduledItem(work=work, command=command))
            return tuple(items)

        # PostgreSQL and non-nested adapters read the pair in a single UoW.
        with self._persistence.transaction() as uow:
            items: list[DurableScheduledItem] = []
            for work in uow.scheduled_work():
                command = uow.get_command(work.command_id)
                if command is None:
                    raise RuntimeError(
                        f"scheduled work references missing command: {work.work_id}/{work.command_id}"
                    )
                items.append(DurableScheduledItem(work=work, command=command))
            return tuple(items)

@dataclass(frozen=True, slots=True)
class RecoveryParticipant:
    """Named durable subsystem participating in runtime reconstruction."""

    name: str
    manager: object

    def validate(self) -> None:
        rebuild = getattr(self.manager, "rebuild_backend", None)
        if not callable(rebuild):
            raise TypeError(
                f"recovery participant {self.name!r} has no rebuild_backend()"
            )
        validate = getattr(self.manager, "validate_rebuild", None)
        if callable(validate):
            validate()

    def rebuild(self, backend: RebuildBackend) -> int:
        rebuild = getattr(self.manager, "rebuild_backend", None)
        if not callable(rebuild):  # pragma: no cover - validated before reconstruction
            raise TypeError(
                f"recovery participant {self.name!r} has no rebuild_backend()"
            )
        restored = rebuild(backend)
        return 0 if restored is None else int(restored)


class RuntimeRebuilder:
    """Reconstruct a fresh runtime through ordered recovery participants."""

    def __init__(
        self,
        persistence: Persistence,
        *,
        context=None,
        participants: tuple[RecoveryParticipant, ...] = (),
    ) -> None:
        self._persistence = persistence
        self._context = context
        self._participants = tuple(participants)

    @property
    def phase_names(self) -> tuple[str, ...]:
        return (
            "context",
            *(participant.name for participant in self._participants),
            "scheduled-work",
        )

    def _restore_context_from_position(self, position) -> None:
        if self._context is None:
            return
        if position is not None:
            self._context.clock.now = position.logical_time
            self._context.clock.tick = position.logical_tick
        self._context.scenarios.restore_state(self._persistence.scenario_state())

    def _validate_scheduled_items(
        self,
        *,
        items: tuple[DurableScheduledItem, ...],
        boundary: datetime,
    ) -> None:
        for item in items:
            if item.work.due_at < boundary:
                raise RuntimeError(
                    f"scheduled work {item.work.work_id} is before recovery boundary"
                )

    @staticmethod
    def _schedule_pending_items(
        backend: RebuildBackend,
        items: tuple[DurableScheduledItem, ...],
        *,
        on_due: Callable[[DurableScheduledItem], None],
    ) -> None:
        for item in items:
            backend.schedule_at(
                item.work.due_at,
                lambda item=item: on_due(item),
                priority=item.work.priority,
                key=("durable-work", item.work.work_id),
            )

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

        # Validate the complete recovery plan before mutating context/backend.
        self._validate_scheduled_items(items=items, boundary=boundary)
        for participant in self._participants:
            participant.validate()

        # Reconstruct in one explicit and stable order.
        self._restore_context_from_position(position)

        for participant in self._participants:
            participant.rebuild(backend)

        self._schedule_pending_items(backend, items, on_due=on_due)
        return len(items)
