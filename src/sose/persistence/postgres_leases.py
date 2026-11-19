"""Writer-lease value types and stale-writer error of the PostgreSQL adapter (from postgres.py)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WriterLease:
    owner_id: str
    epoch: int


class StaleWriterError(RuntimeError):
    """Raised when a PostgreSQL writer loses its fencing epoch."""


@dataclass(frozen=True, slots=True)
class ScopedWriterLease:
    """A writer epoch owned by one job, independent of other job scopes."""

    owner_id: str
    epoch: int
    scope: str


# These types were defined in ``sose.persistence.postgres`` before the split and are
# still re-exported from it. ``__module__`` stays pinned to that public home so class
# paths (codec, pickle, reprs, tracebacks) are unchanged by the move.
# Trade-off: inspect.getsource() on these classes now looks in postgres.py and fails;
# read the definitions here instead.
for _moved in (WriterLease, StaleWriterError, ScopedWriterLease):
    _moved.__module__ = "sose.persistence.postgres"
del _moved
