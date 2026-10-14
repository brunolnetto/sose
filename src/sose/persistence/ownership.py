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

    def _writer_kwargs(self):
        kwargs = {"owner_epoch": self.lease.epoch}
        scope = getattr(self.lease, "scope", None)
        if scope is not None:
            kwargs["writer_scope"] = scope
        return kwargs

    def transaction(self, *, owner_id: str | None = None):
        kwargs = self._writer_kwargs()
        if owner_id is not None:
            if not isinstance(owner_id, str) or not owner_id.strip():
                raise ValueError("owner_id must not be empty")
            kwargs["owner_id"] = owner_id
        return self._persistence.transaction(**kwargs)

    def boundary_transaction(self):
        factory = getattr(self._persistence, "boundary_transaction", None)
        if callable(factory):
            return factory(**self._writer_kwargs())
        return self.transaction()

    def __getattr__(self, name: str):
        return getattr(self._persistence, name)


@dataclass(frozen=True, slots=True)
class PersistentRunResult:
    owner_id: str
    epoch: int
    result: object
