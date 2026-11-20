"""Process-local writer fencing of ``MemoryPersistence``: namespace-wide and
job-scoped writer leases (#637).

Same contract as ``sqlite_incremental_fencing`` and ``postgres_fencing``:

* the namespace-wide writer has one monotonically increasing epoch; once it is
  positive, every unscoped transaction must present the current epoch;
* each scope has its own epoch, independent of the namespace-wide epoch and of
  every other scope, and a scoped transaction validates only its own scope;
* an optional recovery ``correlation_id`` is bound to exactly one scope, for life;
* argument checks, error types and messages match the SQL adapters.

The fences live beside the record state, not in it, like the SQL fencing tables
live beside the record table. They protect one ``MemoryPersistence`` instance
inside one process; they are not durable and do not span processes, so memory
leases are never *authoritative* fencing: ``MemoryPersistence`` declares
``durable_writer_fencing = False`` and probes that need durable fencing
(``ownership.has_durable_writer_fencing``) refuse it.

Memory scoped writers provide the lease/epoch contract for single-process use
and conformance. They do NOT make concurrent writers of different scopes safe
against each other: a memory transaction publishes a whole-state fork, so the
last commit wins over any other transaction that was open at the same time. For
the same reason a claim (and claim-and-register) is refused while a transaction
on the same store is open in the calling context: the outer commit would
silently erase what the claim registered.

One difference from SQLite/PostgreSQL: a claim never waits for in-flight
transactions (there is no database write lock to wait on). Instead, every memory
transaction re-validates its epoch when it publishes, under the same lock as the
claims, so a writer fenced out mid-transaction fails at commit instead of
committing just before the claim. Either way no write of a superseded lease
lands after the claim that superseded it.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class WriterLease:
    """The namespace-wide writer epoch owned by one worker."""

    owner_id: str
    epoch: int


@dataclass(frozen=True, slots=True)
class ScopedWriterLease:
    """A writer epoch owned by one job, independent of other job scopes."""

    owner_id: str
    epoch: int
    scope: str


class StaleWriterError(RuntimeError):
    """Raised when a memory writer claims or commits with an obsolete epoch."""


FENCING_METHODS = (
    "writer_epoch",
    "claim_writer",
    "claim_writer_and_register",
    "scoped_writer_epoch",
    "claim_scoped_writer",
    "claim_scoped_writer_and_register",
)


@dataclass
class _WriterFences:
    lock: threading.Lock = field(default_factory=threading.Lock)
    epoch: int = 0
    owner_id: str | None = None
    scopes: dict[str, tuple[str, int]] = field(default_factory=dict)
    correlation_scopes: dict[str, str] = field(default_factory=dict)
    scope_correlations: dict[str, str] = field(default_factory=dict)

    def scope_epoch(self, scope: str, *, missing: int) -> int:
        entry = self.scopes.get(scope)
        return missing if entry is None else entry[1]

    def require_current(self, owner_epoch: int | None, writer_scope: str | None) -> None:
        """Raise unless the presented epoch is current; the caller holds ``lock``."""
        if writer_scope is not None:
            current = self.scope_epoch(writer_scope, missing=-1)
            if current != owner_epoch:
                raise StaleWriterError(
                    f"stale scoped writer epoch {owner_epoch} for {writer_scope}; "
                    f"current epoch is {current}"
                )
            return
        if self.epoch > 0 and owner_epoch is None:
            raise StaleWriterError(
                f"writer fencing is active at epoch {self.epoch}; owner_epoch is required"
            )
        if owner_epoch is not None and self.epoch != owner_epoch:
            raise StaleWriterError(
                f"stale writer epoch {owner_epoch}; current epoch is {self.epoch}"
            )

    def check_correlation(self, scope: str, correlation_id: str) -> None:
        """Validate the lifelong scope/correlation binding without recording it."""
        original = self.scope_correlations.get(scope)
        if original is not None and original != correlation_id:
            raise ValueError("recovery job's durable organizational binding changed")
        if self.correlation_scopes.get(correlation_id, scope) != scope:
            raise ValueError("recovery correlation is already owned by another job")

    def check_scoped_claim(
        self, scope: str, expected_epoch: int, correlation_id: str | None,
    ) -> int:
        """Validate a scoped claim in SQL order; return the next epoch."""
        if correlation_id is not None:
            self.check_correlation(scope, correlation_id)
        current = self.scope_epoch(scope, missing=0)
        if current != expected_epoch:
            raise StaleWriterError(
                f"scoped writer claim lost for {scope}: expected {expected_epoch}, "
                f"current {current}"
            )
        return current + 1

    def grant_scope(
        self, scope: str, owner_id: str, epoch: int, correlation_id: str | None,
    ) -> ScopedWriterLease:
        if correlation_id is not None:
            self.correlation_scopes[correlation_id] = scope
            self.scope_correlations[scope] = correlation_id
        self.scopes[scope] = (owner_id, epoch)
        return ScopedWriterLease(owner_id, epoch, scope)


class _CommitFence:
    """Re-validates a transaction's epoch while its working state is published."""

    __slots__ = ("_fences", "_owner_epoch", "_writer_scope")

    def __init__(
        self, fences: _WriterFences, owner_epoch: int | None, writer_scope: str | None,
    ) -> None:
        self._fences = fences
        self._owner_epoch = owner_epoch
        self._writer_scope = writer_scope

    @contextmanager
    def publishing(self) -> Iterator[None]:
        with self._fences.lock:
            self._fences.require_current(self._owner_epoch, self._writer_scope)
            yield


def _validate_writer_claim(owner_id: str, expected_epoch: int) -> None:
    if not owner_id:
        raise ValueError("owner_id cannot be empty")
    if expected_epoch < 0:
        raise ValueError("expected_epoch must be >= 0")


def _validate_scoped_claim(
    scope: str, owner_id: str, expected_epoch: int, correlation_id: str | None,
) -> None:
    if not scope or not owner_id:
        raise ValueError("scope and owner must be nonempty")
    if expected_epoch < 0:
        raise ValueError("expected_epoch must be >= 0")
    if correlation_id is not None and not correlation_id:
        raise ValueError("correlation_id must be nonempty")


def _register_initial_job(uow, initial_job) -> None:
    prior = uow.get_job_state(initial_job.job_id)
    if prior is not None:
        if prior.domain_name != initial_job.domain_name:
            raise ValueError("recovery job identity already belongs to another domain")
        if prior.config_json != initial_job.config_json:
            raise ValueError("recovery job's durable organizational binding changed")
    else:
        uow.save_job_state(initial_job)


def hide_inherited_fencing(cls: type, base: type) -> None:
    """Withdraw ``base``'s process-local fencing from a subclass that replaces
    ``transaction`` without honoring it.

    Such a subclass (a file or database adapter) would otherwise advertise
    leases its own transactions never check. The methods become ``None``, so
    capability probes (``callable(getattr(store, "claim_writer", None))``) fail.
    """
    if cls.transaction is base.transaction:
        return
    for name in FENCING_METHODS:
        if getattr(cls, name, None) is getattr(base, name):
            setattr(cls, name, None)


def hide_fencing_on_instance(store) -> None:
    """Make the fencing methods unavailable on one object (read-only views)."""
    for name in FENCING_METHODS:
        setattr(store, name, None)


class MemoryWriterFencingMixin:
    """Writer claims and commit fences; the adapter supplies the transactions."""

    transaction_depth: int

    def _writer_fences(self) -> _WriterFences:
        # Lazy, like the transaction tracker: probes may skip __init__.
        fences = self.__dict__.get("_memory_writer_fences")
        if fences is None:
            fences = self.__dict__.setdefault("_memory_writer_fences", _WriterFences())
        return fences

    def _fences_for_claim(self) -> _WriterFences:
        # Read-only views have no claim methods at all (hide_fencing_on_instance).
        if self.transaction_depth:
            raise RuntimeError(
                "cannot claim a writer lease inside an open transaction on this store; "
                "the transaction's commit would overwrite the claim's registration"
            )
        return self._writer_fences()

    def _commit_fence(self, owner_epoch: int | None, writer_scope: str | None) -> _CommitFence:
        """Validate a transaction's epoch at entry; return the fence for its commit."""
        if writer_scope is not None and (not writer_scope or owner_epoch is None):
            raise ValueError("scoped transactions require scope and writer epoch")
        fences = self._writer_fences()
        with fences.lock:
            fences.require_current(owner_epoch, writer_scope)
        return _CommitFence(fences, owner_epoch, writer_scope)

    def _register_job_unfenced(self, initial_job) -> None:
        """Publish the job registration; the caller holds the fence lock."""
        from .memory import MemoryUnitOfWork, fork_state

        uow = MemoryUnitOfWork(fork_state(self._state), self)
        _register_initial_job(uow, initial_job)
        uow.commit()

    def writer_epoch(self) -> int:
        return self._writer_fences().epoch

    def claim_writer(self, owner_id: str, *, expected_epoch: int) -> WriterLease:
        """Compare-and-swap the namespace-wide epoch; scoped epochs are untouched."""
        return self._claim_writer(owner_id, expected_epoch, initial_job=None)

    def claim_writer_and_register(
        self, owner_id: str, *, expected_epoch: int, initial_job,
    ) -> WriterLease:
        """Claim the namespace-wide epoch and register the job, or do neither."""
        return self._claim_writer(owner_id, expected_epoch, initial_job=initial_job)

    def _claim_writer(self, owner_id: str, expected_epoch: int, *, initial_job) -> WriterLease:
        _validate_writer_claim(owner_id, expected_epoch)
        fences = self._fences_for_claim()
        with fences.lock:
            if fences.epoch != expected_epoch:
                raise StaleWriterError(f"writer claim lost: expected epoch {expected_epoch}")
            if initial_job is not None:
                self._register_job_unfenced(initial_job)
            fences.epoch += 1
            fences.owner_id = owner_id
            return WriterLease(owner_id=owner_id, epoch=fences.epoch)

    def scoped_writer_epoch(self, scope: str) -> int:
        if not scope:
            raise ValueError("writer scope must be nonempty")
        return self._writer_fences().scope_epoch(scope, missing=0)

    def claim_scoped_writer(
        self,
        scope: str,
        owner_id: str,
        *,
        expected_epoch: int,
        correlation_id: str | None = None,
    ) -> ScopedWriterLease:
        return self._claim_scope(scope, owner_id, expected_epoch, correlation_id, None)

    def claim_scoped_writer_and_register(
        self,
        scope: str,
        owner_id: str,
        *,
        expected_epoch: int,
        correlation_id: str | None,
        initial_job,
    ) -> ScopedWriterLease:
        """Commit the scoped claim, correlation binding and job ID together or not at all."""
        return self._claim_scope(scope, owner_id, expected_epoch, correlation_id, initial_job)

    def _claim_scope(
        self,
        scope: str,
        owner_id: str,
        expected_epoch: int,
        correlation_id: str | None,
        initial_job,
    ) -> ScopedWriterLease:
        _validate_scoped_claim(scope, owner_id, expected_epoch, correlation_id)
        fences = self._fences_for_claim()
        with fences.lock:
            epoch = fences.check_scoped_claim(scope, expected_epoch, correlation_id)
            if initial_job is not None:
                self._register_job_unfenced(initial_job)
            return fences.grant_scope(scope, owner_id, epoch, correlation_id)
