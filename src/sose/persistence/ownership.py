"""Ownership-aware Engine OLTP views used by persistent job runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class WriterLeaseLike(Protocol):
    owner_id: str
    epoch: int


class WriterOwnershipPersistence(Protocol):
    def writer_epoch(self) -> int: ...
    def claim_writer(self, owner_id: str, *, expected_epoch: int) -> WriterLeaseLike: ...


class FencedEnginePersistence:
    """Delegate reads while injecting the acquired epoch into every transaction."""
    def __init__(self, persistence: Any, lease: WriterLeaseLike) -> None:
        self._persistence = persistence
        self.lease = lease

    def transaction(self):
        return self._persistence.transaction(owner_epoch=self.lease.epoch)

    def __getattr__(self, name: str):
        return getattr(self._persistence, name)


@dataclass(frozen=True, slots=True)
class PersistentRunResult:
    owner_id: str
    epoch: int
    result: object
