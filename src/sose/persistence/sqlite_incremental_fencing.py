"""Writer fencing helpers of the incremental SQLite adapter: job registration and
job-scoped writer leases (#637).

Same contract as ``postgres_fencing.PostgresWriterFencingMixin``:

* each scope has its own monotonically increasing epoch, independent of the
  namespace-wide writer epoch and of every other scope;
* an optional recovery ``correlation_id`` is bound to exactly one scope, for life;
* a scoped transaction validates only its own scope's epoch.

SQLite serializes every write transaction with ``BEGIN IMMEDIATE``, so the
PostgreSQL advisory locks have no counterpart here: claims and scoped writes of
different scopes are simply ordered by the database write lock. The tables are
additive, so older SOSE versions can still open a database that has them.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .memory import MemoryUnitOfWork, fork_state


@dataclass(frozen=True, slots=True)
class ScopedWriterLease:
    """A writer epoch owned by one job, independent of other job scopes."""

    owner_id: str
    epoch: int
    scope: str


def _stale(message: str) -> Exception:
    # Imported lazily: sqlite_incremental imports this module.
    from .sqlite_incremental import StaleWriterError

    return StaleWriterError(message)


class SQLiteIncrementalWriterFencingMixin:
    """Job registration plus scoped-writer claims and epoch checks.

    The adapter supplies the connection, revision and record-diff machinery.
    """

    # The leases live in the database: authoritative fencing (#637). Precedes
    # MemoryPersistence (durable_writer_fencing = False) in the adapter's MRO.
    durable_writer_fencing = True

    _connection: sqlite3.Connection
    _scoped_writers_table: str
    _correlation_owners_table: str

    @staticmethod
    def _register_initial_job(uow: MemoryUnitOfWork, initial_job) -> None:
        prior = uow.get_job_state(initial_job.job_id)
        if prior is not None:
            if prior.domain_name != initial_job.domain_name:
                raise ValueError("recovery job identity already belongs to another domain")
            if prior.config_json != initial_job.config_json:
                raise ValueError("recovery job's durable organizational binding changed")
        else:
            uow.save_job_state(initial_job)

    def _persist_job_registration(self, initial_job) -> int:
        """Inside an open BEGIN IMMEDIATE: register the job, return the next revision.

        On failure the in-memory state is restored; the caller rolls back SQLite.
        """
        self._refresh_from_db()
        before = self._state
        try:
            uow = MemoryUnitOfWork(fork_state(self._state), self)
            self._register_initial_job(uow, initial_job)
            uow.commit()
            changed = self._apply_changes(before, self._state, uow.dirty_records)
            next_revision = self._revision
            if changed:
                next_revision += 1
                self._connection.execute(
                    f"UPDATE {self._meta_table} SET revision = ? WHERE singleton = 1",
                    (next_revision,),
                )
            return next_revision
        except Exception:
            self._state = before
            raise

    def _create_scoped_writer_tables(self) -> None:
        self._connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._scoped_writers_table} (
                scope TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                owner_epoch INTEGER NOT NULL CHECK (owner_epoch >= 1)
            )
            """
        )
        self._connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._correlation_owners_table} (
                correlation_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL UNIQUE
            )
            """
        )

    def scoped_writer_epoch(self, scope: str) -> int:
        if not scope:
            raise ValueError("writer scope must be nonempty")
        row = self._connection.execute(
            f"SELECT owner_epoch FROM {self._scoped_writers_table} WHERE scope = ?",
            (scope,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    @staticmethod
    def _validate_scoped_claim(
        scope: str, owner_id: str, expected_epoch: int, correlation_id: str | None,
    ) -> None:
        if not scope or not owner_id:
            raise ValueError("scope and owner must be nonempty")
        if expected_epoch < 0:
            raise ValueError("expected_epoch must be >= 0")
        if correlation_id is not None and not correlation_id:
            raise ValueError("correlation_id must be nonempty")

    def _claim_scope_in_open_transaction(
        self, scope: str, owner_id: str, expected_epoch: int, correlation_id: str | None,
    ) -> ScopedWriterLease:
        """Run inside an open ``BEGIN IMMEDIATE``; the caller commits or rolls back."""
        if correlation_id is not None:
            self._reserve_recovery_correlation(scope, correlation_id)
        current = self.scoped_writer_epoch(scope)
        if current != expected_epoch:
            raise _stale(
                f"scoped writer claim lost for {scope}: expected {expected_epoch}, "
                f"current {current}"
            )
        self._connection.execute(
            f"""
            INSERT INTO {self._scoped_writers_table} (scope, owner_id, owner_epoch)
            VALUES (?, ?, ?)
            ON CONFLICT (scope) DO UPDATE
            SET owner_id = excluded.owner_id, owner_epoch = excluded.owner_epoch
            """,
            (scope, owner_id, current + 1),
        )
        return ScopedWriterLease(owner_id, current + 1, scope)

    def _reserve_recovery_correlation(self, scope: str, correlation_id: str) -> None:
        """Bind ``correlation_id`` to ``scope`` inside the caller's claim transaction."""
        original = self._connection.execute(
            f"SELECT correlation_id FROM {self._correlation_owners_table} WHERE scope = ?",
            (scope,),
        ).fetchone()
        if original is not None and original[0] != correlation_id:
            raise ValueError("recovery job's durable organizational binding changed")
        self._connection.execute(
            f"INSERT INTO {self._correlation_owners_table} (correlation_id, scope) "
            "VALUES (?, ?) ON CONFLICT (correlation_id) DO NOTHING",
            (correlation_id, scope),
        )
        owner = self._connection.execute(
            f"SELECT scope FROM {self._correlation_owners_table} WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        if owner is None or owner[0] != scope:
            raise ValueError("recovery correlation is already owned by another job")

    def claim_scoped_writer(
        self,
        scope: str,
        owner_id: str,
        *,
        expected_epoch: int,
        correlation_id: str | None = None,
    ) -> ScopedWriterLease:
        self._validate_scoped_claim(scope, owner_id, expected_epoch, correlation_id)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            lease = self._claim_scope_in_open_transaction(
                scope, owner_id, expected_epoch, correlation_id,
            )
            self._connection.commit()
            return lease
        except Exception:
            self._connection.rollback()
            raise

    def claim_scoped_writer_and_register(
        self,
        scope: str,
        owner_id: str,
        *,
        expected_epoch: int,
        correlation_id: str | None,
        initial_job,
    ) -> ScopedWriterLease:
        """One BEGIN IMMEDIATE commits the scoped claim, correlation binding and job ID."""
        self._validate_scoped_claim(scope, owner_id, expected_epoch, correlation_id)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            lease = self._claim_scope_in_open_transaction(
                scope, owner_id, expected_epoch, correlation_id,
            )
            next_revision = self._persist_job_registration(initial_job)
            self._connection.commit()
            self._revision = next_revision
            return lease
        except Exception:
            self._connection.rollback()
            raise

    def _require_current_scoped_epoch(self, owner_epoch: int, writer_scope: str) -> None:
        """Roll back the open transaction and raise unless the scope's epoch is current."""
        row = self._connection.execute(
            f"SELECT owner_epoch FROM {self._scoped_writers_table} WHERE scope = ?",
            (writer_scope,),
        ).fetchone()
        current = -1 if row is None else int(row[0])
        if current != owner_epoch:
            self._connection.rollback()
            raise _stale(
                f"stale scoped writer epoch {owner_epoch} for {writer_scope}; "
                f"current epoch is {current}"
            )
