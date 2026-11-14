"""Remote PostgreSQL record-level persistence adapter.

Cohesive parts live in sibling modules and are re-exported here: lease types
(postgres_leases), schema bootstrap (postgres_schema), the dirty-record write path
(postgres_writes), writer fencing (postgres_fencing) and refresh-before-read
queries (postgres_reads).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass  # noqa: F401
from datetime import datetime, timezone
import re
from typing import Iterator

import psycopg
from psycopg import sql

from sose.core.resource_identity import ResourceAddress

from .codec import dumps, loads  # noqa: F401
from .memory import (
    tracked_transaction_depth,
    MemoryPersistence,  # noqa: F401
    MemoryUnitOfWork,
    _State,
    fork_state,
)
from .postgres_fencing import PostgresWriterFencingMixin
from .postgres_leases import ScopedWriterLease, StaleWriterError, WriterLease  # noqa: F401
from .postgres_reads import PostgresFreshReadsMixin
from .postgres_schema import (
    _SCHEMA_VERSION,  # noqa: F401
    bootstrap_schema,
    committed_schema_is_current,
)
from .postgres_writes import PostgresRecordWritesMixin
from .records import StateRecord, changes_for_dirty_records, records_to_state  # noqa: F401


_NAMESPACE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")


class PostgresPersistence(
    PostgresRecordWritesMixin,
    PostgresWriterFencingMixin,
    PostgresFreshReadsMixin,
):
    """Remote PostgreSQL record-level persistence.

    The adapter reuses SOSE's backend-neutral record/delta mapping. Independent
    connections may transact concurrently; disjoint dirty records do not
    overwrite each other. Same-record conflicts are currently last-writer-wins.
    """

    def __init__(
        self,
        dsn: str,
        *,
        namespace: str = "sose",
    ) -> None:
        super().__init__()
        if not dsn:
            raise ValueError("PostgresPersistence dsn cannot be empty")
        if not _NAMESPACE_RE.fullmatch(namespace):
            raise ValueError(
                "PostgresPersistence namespace must be a SQL-safe identifier "
                "with at most 40 characters"
            )

        self.dsn = dsn
        self.namespace = namespace
        self._meta_table = sql.Identifier(f"{namespace}_record_meta")
        self._record_table = sql.Identifier(f"{namespace}_record")
        self._scoped_writers_table = sql.Identifier(f"{namespace}_writer_scope")
        self._correlation_owners_table = sql.Identifier(f"{namespace}_recovery_owner")
        self._connection = psycopg.connect(dsn, autocommit=True)

        # Fast path for an *already committed* namespace: a new independent
        # organization must not wait for an unrelated owner's open transaction
        # merely because its adapter constructor would otherwise re-run DDL.
        # Schema bootstrap/migration still uses the exclusive namespace lock,
        # so concurrent first-time provisioners remain serialized.
        if not committed_schema_is_current(self._connection, namespace, self._meta_table):
            bootstrap_schema(
                self._connection,
                self.namespace,
                meta_table=self._meta_table,
                record_table=self._record_table,
                scoped_writers_table=self._scoped_writers_table,
                correlation_owners_table=self._correlation_owners_table,
            )

        self._revision = -1
        self._refresh_from_db(force=True)

    def trusted_utc_now(self) -> datetime:
        """Return authoritative database wall time for cross-host lease fencing.

        SELECT clock_timestamp() is evaluated at the call, not the beginning
        of a possibly long-lived transaction, unlike transaction_timestamp().
        Call while holding the job-scoped advisory guard immediately before
        validating or writing lease expiry. Worker-supplied clocks are ignored.
        """
        record = self._connection.execute("SELECT clock_timestamp()").fetchone()
        if record is None or not isinstance(record[0], datetime):
            raise RuntimeError("PostgreSQL trusted clock unavailable")
        return record[0].astimezone(timezone.utc)

    @contextmanager
    def business_resource_guard(
        self,
        resource_name: str | ResourceAddress,
    ) -> Iterator[None]:
        """Coordinate a multi-commit domain operation on one resource identity.

        Typed addresses distinguish locally owned pools, shared pools and
        physical instances. Existing string keys retain their exact locking
        semantics. This guard serializes an operation but is not a durable
        reservation or proof of capacity allocation.
        """
        if isinstance(resource_name, ResourceAddress):
            key = resource_name.lock_key()
        elif isinstance(resource_name, str) and resource_name:
            key = resource_name
        else:
            raise ValueError("resource_name must be a nonempty name or ResourceAddress")
        self._connection.execute(
            "SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))",
            (self.namespace, key),
        )
        try:
            yield
        finally:
            self._connection.execute(
                "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                (self.namespace, key),
            )

    def temporal_resources(self, *, bootstrap: bool = True):
        """Access the opt-in authoritative PostgreSQL temporal resource ledger.

        Mutations and resource causal receipts commit atomically within the
        ledger. They are not automatically part of a separate engine UoW.
        Set bootstrap=False only for pre-registered read-side queries:
        skip DDL and namespace advisory locks, fail if tables are absent.
        """
        from .postgres_resource_ledger import PostgresTemporalResourceLedger

        return PostgresTemporalResourceLedger(self, bootstrap=bootstrap)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PostgresPersistence":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _database_revision(self) -> int:
        row = self._connection.execute(
            sql.SQL("SELECT revision FROM {} WHERE singleton = 1").format(self._meta_table)
        ).fetchone()
        if row is None:
            raise RuntimeError("PostgresPersistence metadata row is missing")
        return int(row[0])

    def _load_records_from_db(self) -> _State:
        rows = self._connection.execute(
            sql.SQL(
                """
                SELECT collection, record_key, position, payload
                FROM {}
                ORDER BY collection, position, record_key
                """
            ).format(self._record_table)
        ).fetchall()
        records = (
            StateRecord(
                collection=str(collection),
                key=str(record_key),
                position=int(position),
                payload=str(payload),
            )
            for collection, record_key, position, payload in rows
        )
        return records_to_state(records)

    def _refresh_from_db(self, *, force: bool = False) -> None:
        revision = self._database_revision()
        if not force and revision == self._revision:
            return
        self._state = self._load_records_from_db()
        self._revision = revision

    def _snapshot_state(self) -> _State:
        with self._connection.transaction():
            self._connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            revision = self._database_revision()
            if revision == self._revision:
                return self._state
            return self._load_records_from_db()

    def boundary_protocol_snapshot(self):
        """Read all causal boundary records in ONE fence-neutral SQL snapshot.

        PostgreSQL's global/scoped writer epoch protects mutations, not
        observational queries. REPEATABLE READ keeps revision and rows from
        one committed snapshot even when another worker is publishing.
        Unlike transaction(), this never persists a UnitOfWork or acquires a
        writer lease. No process-local cache is used as evidence.
        """
        with self._connection.transaction():
            self._connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            self._refresh_from_db(force=True)
            uow = MemoryUnitOfWork(fork_state(self._state), self)
            try:
                return (
                    uow.boundary_messages(),
                    uow.boundary_deliveries(),
                    uow.boundary_consumptions(),
                )
            finally:
                uow.rollback()

    @contextmanager
    def boundary_transaction(
        self,
        *,
        owner_epoch: int | None = None,
        writer_scope: str | None = None,
    ) -> Iterator[MemoryUnitOfWork]:
        """Serialize overlapping causal boundary writes, not unrelated jobs.

        Legacy calls exclusively lock the whole namespace. Scoped jobs take
        the compatible shared namespace lock plus an exclusive per-job lock.
        This lets independent correlations claim/ACK concurrently while any
        legacy unscoped writer remains mutually exclusive with scoped writers.
        """
        with self._connection.transaction():
            boundary_namespace = f"{self.namespace}:causal-boundary"
            if writer_scope is None:
                self._connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (boundary_namespace,),
                )
            else:
                self._connection.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
                    (boundary_namespace,),
                )
                self._connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (boundary_namespace, "job:" + writer_scope),
                )
            with self.transaction(
                owner_epoch=owner_epoch,
                writer_scope=writer_scope,
            ) as uow:
                yield uow

    @tracked_transaction_depth
    @contextmanager
    def transaction(
        self,
        *,
        owner_epoch: int | None = None,
        writer_scope: str | None = None,
        owner_id: str | None = None,
    ) -> Iterator[MemoryUnitOfWork]:
        if owner_id is not None and not owner_id:
            raise ValueError("owner_id must not be empty")
        before: _State | None = None
        try:
            with self._connection.transaction():
                if owner_id is not None:
                    # Per-owner admission linearizes clock/sequence read and write.
                    # Independent owners never contend on this advisory key.
                    self._connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                        (self.namespace, "runtime-owner:" + owner_id),
                    )
                if writer_scope is None:
                    # Preserve the original single-argument extension point.
                    self._lock_and_validate_epoch(owner_epoch)
                else:
                    self._lock_and_validate_epoch(
                        owner_epoch,
                        writer_scope=writer_scope,
                    )
                before, uow = self._begin_transaction_uow()
                yield uow
                if not uow._closed:
                    uow.commit()

                changed = self._apply_changes(before, self._state, uow.dirty_records)
                next_revision = self._increment_revision() if changed else self._revision
            # A concurrent writer may have committed records that were not
            # present in this transaction's in-memory snapshot before our
            # revision increment. Never claim the local cache represents the
            # returned global revision; force the next read to refresh.
            self._revision = -1 if changed else next_revision
        except Exception:
            if before is not None:
                self._state = before
            raise

    def _begin_transaction_uow(self) -> tuple[_State, MemoryUnitOfWork]:
        # Force a transaction-consistent refresh. Writers may overlap, and
        # dirty-record persistence prevents unrelated updates from replacing one another.
        self._refresh_from_db(force=True)
        before = self._state
        return before, MemoryUnitOfWork(fork_state(self._state), self)

    def _increment_revision(self) -> int:
        row = self._connection.execute(
            sql.SQL(
                """
                UPDATE {}
                SET revision = revision + 1
                WHERE singleton = 1
                RETURNING revision
                """
            ).format(self._meta_table)
        ).fetchone()
        if row is None:  # pragma: no cover - metadata singleton invariant
            raise RuntimeError("PostgresPersistence revision update failed")
        return int(row[0])

    def persisted_record_count(self) -> int:
        row = self._connection.execute(
            sql.SQL("SELECT COUNT(*) FROM {}").format(self._record_table)
        ).fetchone()
        return 0 if row is None else int(row[0])
