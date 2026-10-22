from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Iterator

import psycopg
from psycopg import sql

from sose.core.resource_identity import ResourceAddress

from .codec import dumps, loads
from .memory import tracked_transaction_depth, MemoryPersistence, MemoryUnitOfWork, _State, fork_state
from .records import StateRecord, changes_for_dirty_records, records_to_state


_SCHEMA_VERSION = 2

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

_NAMESPACE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")


class PostgresPersistence(MemoryPersistence):
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
        # Schema bootstrap/migration still uses the exclusive namespace lock
        # below, so concurrent first-time provisioners remain serialized.
        relations = self._connection.execute(
            "SELECT to_regclass(%s), to_regclass(%s), "
            "to_regclass(%s), to_regclass(%s)",
            (
                f"{namespace}_record_meta",
                f"{namespace}_record",
                f"{namespace}_writer_scope",
                f"{namespace}_recovery_owner",
            ),
        ).fetchone()
        if relations is not None and all(relation is not None for relation in relations):
            committed_meta = self._connection.execute(
                sql.SQL("SELECT schema_version FROM {} WHERE singleton = 1").format(
                    self._meta_table
                )
            ).fetchone()
            if committed_meta is not None and int(committed_meta[0]) == _SCHEMA_VERSION:
                self._revision = -1
                self._refresh_from_db(force=True)
                return

        with self._connection.transaction():
            # CREATE TABLE IF NOT EXISTS does not serialize concurrent first
            # creation of PostgreSQL composite types/catalog rows. Use the
            # same namespace advisory lock as boundary/epoch writers, in its
            # exclusive form, before any schema creation or migration.
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (self.namespace,),
            )
            self._connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        revision BIGINT NOT NULL DEFAULT 0,
                        owner_id TEXT,
                        owner_epoch BIGINT NOT NULL DEFAULT 0
                    )
                    """
                ).format(self._meta_table)
            )
            self._connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        collection TEXT NOT NULL,
                        record_key TEXT NOT NULL,
                        position BIGINT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (collection, record_key)
                    )
                    """
                ).format(self._record_table)
            )
            self._connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {}(singleton, schema_version, revision)
                    VALUES (1, %s, 0)
                    ON CONFLICT(singleton) DO NOTHING
                    """
                ).format(self._meta_table),
                (_SCHEMA_VERSION,),
            )
            self._connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        scope TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        owner_epoch BIGINT NOT NULL CHECK (owner_epoch >= 1)
                    )
                    """
                ).format(self._scoped_writers_table)
            )
            self._connection.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {} (
                        correlation_id TEXT PRIMARY KEY,
                        scope TEXT NOT NULL UNIQUE
                    )
                    """
                ).format(self._correlation_owners_table)
            )
            row = self._connection.execute(
                sql.SQL(
                    "SELECT schema_version FROM {} WHERE singleton = 1"
                ).format(self._meta_table)
            ).fetchone()
            if row is None:  # pragma: no cover - defensive database invariant
                raise RuntimeError("PostgresPersistence metadata bootstrap failed")
            version = int(row[0])
            if version == 1:
                self._connection.execute(
                    sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS owner_id TEXT").format(
                        self._meta_table
                    )
                )
                self._connection.execute(
                    sql.SQL(
                        "ALTER TABLE {} ADD COLUMN IF NOT EXISTS "
                        "owner_epoch BIGINT NOT NULL DEFAULT 0"
                    ).format(self._meta_table)
                )
                self._connection.execute(
                    sql.SQL("UPDATE {} SET schema_version = 2 WHERE singleton = 1").format(
                        self._meta_table
                    )
                )
                version = 2
            if version != _SCHEMA_VERSION:
                raise RuntimeError(
                    "unsupported PostgresPersistence schema version: "
                    f"{version}"
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
        self, resource_name: str | ResourceAddress,
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
            sql.SQL("SELECT revision FROM {} WHERE singleton = 1").format(
                self._meta_table
            )
        ).fetchone()
        if row is None:
            raise RuntimeError("PostgresPersistence metadata row is missing")
        return int(row[0])

    def _refresh_from_db(self, *, force: bool = False) -> None:
        revision = self._database_revision()
        if not force and revision == self._revision:
            return

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
        self._state = records_to_state(records)
        self._revision = revision

    def _apply_changes(
        self,
        before: _State,
        after: _State,
        dirty_records,
    ) -> int:
        changes = changes_for_dirty_records(before, after, dirty_records)
        event_appends = tuple(change for change in changes if change.collection == "events")
        for change in changes:
            if change.collection == "events":
                # Appends live in one shared log even when source/job fences
                # belong to independent organizations. Their index in the
                # UoW snapshot is NOT a globally unique record key.
                continue
            if (
                change.collection == "simulation_positions"
                and change.operation == "upsert"
            ):
                # Each owner has its own authoritative recovery boundary. A
                # writer must atomically replace exactly the position it read,
                # not overwrite a newer one committed by another connection.
                assert change.position is not None
                assert change.payload is not None
                owner_id = loads(change.key)
                if not isinstance(owner_id, str) or not owner_id:
                    raise ValueError("simulation position requires an owner key")
                expected = before.simulation_positions.get(owner_id)
                if expected is None:
                    result = self._connection.execute(
                        sql.SQL(
                            """
                            INSERT INTO {}(collection, record_key, position, payload)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT(collection, record_key) DO NOTHING
                            RETURNING record_key
                            """
                        ).format(self._record_table),
                        (change.collection, change.key, change.position, change.payload),
                    )
                else:
                    result = self._connection.execute(
                        sql.SQL(
                            """
                            UPDATE {} SET position = %s, payload = %s
                            WHERE collection = %s AND record_key = %s
                              AND payload = %s
                            RETURNING record_key
                            """
                        ).format(self._record_table),
                        (
                            change.position, change.payload,
                            change.collection, change.key, dumps(expected),
                        ),
                    )
                if result.fetchone() is None:
                    raise StaleWriterError(
                        f"stale simulation position for owner {owner_id!r}"
                    )
                continue
            if change.operation == "delete":
                self._connection.execute(
                    sql.SQL(
                        "DELETE FROM {} WHERE collection = %s AND record_key = %s"
                    ).format(self._record_table),
                    (change.collection, change.key),
                )
            elif change.operation == "upsert":
                assert change.position is not None
                assert change.payload is not None
                self._connection.execute(
                    sql.SQL(
                        """
                        INSERT INTO {}(collection, record_key, position, payload)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT(collection, record_key) DO UPDATE SET
                            position = EXCLUDED.position,
                            payload = EXCLUDED.payload
                        """
                    ).format(self._record_table),
                    (
                        change.collection,
                        change.key,
                        change.position,
                        change.payload,
                    ),
                )
            else:  # pragma: no cover - closed record operation set
                raise RuntimeError(
                    f"unknown state record operation: {change.operation}"
                )
        if event_appends:
            self._append_postgres_events(before, after, event_appends)
        return len(changes)

    def _append_postgres_events(
        self, before: _State, after: _State, changes,
    ) -> None:
        """Linearize only the shared event-log tail, never organizational work.

        Separate organization transactions may read the same event count.
        The PostgreSQL append critical section allocates monotonically
        increasing positions at COMMIT; record identity is the event ID.
        A failed/duplicate append aborts its enclosing source+checkpoint UoW.
        """
        self._connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            (self.namespace, "durable:event-append"),
        )
        row = self._connection.execute(
            sql.SQL(
                "SELECT COALESCE(MAX(position), -1) FROM {} WHERE collection = %s"
            ).format(self._record_table),
            ("events",),
        ).fetchone()
        assert row is not None
        next_position = int(row[0]) + 1
        for change in sorted(changes, key=lambda item: item.position):
            if (
                change.operation != "upsert"
                or change.position is None
                or change.position < len(before.events)
                or change.position >= len(after.events)
            ):
                raise ValueError("PostgreSQL durable events are append-only")
            event = after.events[change.position]
            if not event.event_id:
                raise ValueError("PostgreSQL durable event ID cannot be empty")
            # Exact deterministic replay is idempotent; a reused identity
            # with changed causal facts is not. Inspect legacy numeric-key
            # events in this UoW's snapshot as well as new identity keys.
            earlier = tuple(
                item for item in before.events if item.event_id == event.event_id
            )
            if earlier:
                if any(item != event for item in earlier):
                    raise ValueError(
                        f"conflicting PostgreSQL durable event ID: {event.event_id}"
                    )
                continue
            payload = dumps(event)
            key = "event:" + event.event_id
            stored = self._connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {}(collection, record_key, position, payload)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(collection, record_key) DO NOTHING
                    RETURNING record_key
                    """
                ).format(self._record_table),
                ("events", key, next_position, payload),
            ).fetchone()
            if stored is None:
                original = self._connection.execute(
                    sql.SQL(
                        "SELECT payload FROM {} WHERE collection = %s AND record_key = %s"
                    ).format(self._record_table),
                    ("events", key),
                ).fetchone()
                if original is None or loads(str(original[0])) != event:
                    raise ValueError(
                        f"conflicting PostgreSQL durable event ID: {event.event_id}"
                    )
                continue
            next_position += 1

    def writer_epoch(self) -> int:
        row = self._connection.execute(
            sql.SQL("SELECT owner_epoch FROM {} WHERE singleton = 1").format(
                self._meta_table
            )
        ).fetchone()
        if row is None:
            raise RuntimeError("writer metadata disappeared")
        return int(row[0])

    def claim_writer(self, owner_id: str, *, expected_epoch: int) -> WriterLease:
        if not owner_id:
            raise ValueError("owner_id cannot be empty")
        if expected_epoch < 0:
            raise ValueError("expected_epoch must be >= 0")
        with self._connection.transaction():
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (self.namespace,),
            )
            row = self._connection.execute(
                sql.SQL(
                    """
                    UPDATE {}
                    SET owner_id = %s, owner_epoch = owner_epoch + 1
                    WHERE singleton = 1 AND owner_epoch = %s
                    RETURNING owner_epoch
                    """
                ).format(self._meta_table),
                (owner_id, expected_epoch),
            ).fetchone()
            if row is None:
                raise StaleWriterError(
                    f"writer claim lost: expected epoch {expected_epoch}"
                )
            return WriterLease(owner_id=owner_id, epoch=int(row[0]))

    def claim_writer_and_register(
        self, owner_id: str, *, expected_epoch: int, initial_job,
    ) -> WriterLease:
        """Atomically fence the writer AND persist a previously absent job ID.

        A crash after this call may leave a pending job, but never an orphaned
        positive writer epoch with no authoritative domain identity.
        """
        with self._connection.transaction():
            lease = self.claim_writer(owner_id, expected_epoch=expected_epoch)
            with self.transaction(owner_epoch=lease.epoch) as uow:
                prior = uow.get_job_state(initial_job.job_id)
                if prior is not None:
                    if prior.domain_name != initial_job.domain_name:
                        raise ValueError("recovery job identity already belongs to another domain")
                    if prior.config_json != initial_job.config_json:
                        raise ValueError("recovery job's durable organizational binding changed")
                else:
                    uow.save_job_state(initial_job)
            return lease

    def scoped_writer_epoch(self, scope: str) -> int:
        if not scope:
            raise ValueError("writer scope must be nonempty")
        row = self._connection.execute(
            sql.SQL("SELECT owner_epoch FROM {} WHERE scope = %s").format(
                self._scoped_writers_table
            ),
            (scope,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def claim_scoped_writer(
        self, scope: str, owner_id: str, *, expected_epoch: int,
        correlation_id: str | None = None,
    ) -> ScopedWriterLease:
        if not scope or not owner_id:
            raise ValueError("scope and owner must be nonempty")
        if expected_epoch < 0:
            raise ValueError("expected_epoch must be >= 0")
        if correlation_id is not None and not correlation_id:
            raise ValueError("correlation_id must be nonempty")
        with self._connection.transaction():
            # Only workers claiming this same scope serialize. Do not use the
            # namespace-wide writer epoch or lock for independent jobs.
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (self.namespace, "writer-scope:" + scope),
            )
            if correlation_id is not None:
                original = self._connection.execute(
                    sql.SQL(
                        "SELECT correlation_id FROM {} WHERE scope = %s"
                    ).format(self._correlation_owners_table),
                    (scope,),
                ).fetchone()
                if original is not None and original[0] != correlation_id:
                    raise ValueError(
                        "recovery job's durable organizational binding changed"
                    )
                self._connection.execute(
                    sql.SQL(
                        "INSERT INTO {} (correlation_id, scope) VALUES (%s, %s) "
                        "ON CONFLICT (correlation_id) DO NOTHING"
                    ).format(self._correlation_owners_table),
                    (correlation_id, scope),
                )
                owner = self._connection.execute(
                    sql.SQL(
                        "SELECT scope FROM {} WHERE correlation_id = %s"
                    ).format(self._correlation_owners_table),
                    (correlation_id,),
                ).fetchone()
                if owner is None or owner[0] != scope:
                    raise ValueError(
                        "recovery correlation is already owned by another job"
                    )
            row = self._connection.execute(
                sql.SQL("SELECT owner_epoch FROM {} WHERE scope = %s").format(
                    self._scoped_writers_table
                ), (scope,),
            ).fetchone()
            current = 0 if row is None else int(row[0])
            if current != expected_epoch:
                raise StaleWriterError(
                    f"scoped writer claim lost for {scope}: expected {expected_epoch}, "
                    f"current {current}"
                )
            self._connection.execute(
                sql.SQL(
                    """
                    INSERT INTO {} (scope, owner_id, owner_epoch)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (scope) DO UPDATE
                    SET owner_id=EXCLUDED.owner_id, owner_epoch=EXCLUDED.owner_epoch
                    """
                ).format(self._scoped_writers_table),
                (scope, owner_id, current + 1),
            )
            return ScopedWriterLease(owner_id, current + 1, scope)

    def claim_scoped_writer_and_register(
        self, scope: str, owner_id: str, *, expected_epoch: int,
        correlation_id: str | None, initial_job,
    ) -> ScopedWriterLease:
        """Commit scoped claim/correlation reservation and job ID together."""
        with self._connection.transaction():
            lease = self.claim_scoped_writer(
                scope, owner_id, expected_epoch=expected_epoch,
                correlation_id=correlation_id,
            )
            with self.transaction(owner_epoch=lease.epoch, writer_scope=scope) as uow:
                prior = uow.get_job_state(initial_job.job_id)
                if prior is not None:
                    if prior.domain_name != initial_job.domain_name:
                        raise ValueError("recovery job identity already belongs to another domain")
                    if prior.config_json != initial_job.config_json:
                        raise ValueError("recovery job's durable organizational binding changed")
                else:
                    uow.save_job_state(initial_job)
            return lease

    def boundary_protocol_snapshot(self):
        """Read all causal boundary records in ONE fence-neutral SQL snapshot.

        PostgreSQL's global/scoped writer epoch protects mutations, not
        observational queries. REPEATABLE READ keeps revision and rows from
        one committed snapshot even when another worker is publishing.
        Unlike transaction(), this never persists a UnitOfWork or acquires a
        writer lease. No process-local cache is used as evidence.
        """
        with self._connection.transaction():
            self._connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
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
        self, *, owner_epoch: int | None = None,
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
                owner_epoch=owner_epoch, writer_scope=writer_scope,
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
                        owner_epoch, writer_scope=writer_scope,
                    )
                before, uow = self._begin_transaction_uow()
                yield uow
                if not uow._closed:
                    uow.commit()

                changed = self._apply_changes(before, self._state, uow.dirty_records)
                next_revision = (
                    self._increment_revision()
                    if changed
                    else self._revision
                )
            # A concurrent writer may have committed records that were not
            # present in this transaction's in-memory snapshot before our
            # revision increment. Never claim the local cache represents the
            # returned global revision; force the next read to refresh.
            self._revision = -1 if changed else next_revision
        except Exception:
            if before is not None:
                self._state = before
            raise

    def _lock_and_validate_epoch(
        self, owner_epoch: int | None, *, writer_scope: str | None = None,
    ) -> int:
        if writer_scope is not None:
            if not writer_scope or owner_epoch is None:
                raise ValueError("scoped transactions require scope and writer epoch")
            # A scope's exclusive claim waits for its in-flight writes, but
            # cannot preempt or block transactions from another job scope.
            self._connection.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtext(%s), hashtext(%s))",
                (self.namespace, "writer-scope:" + writer_scope),
            )
            row = self._connection.execute(
                sql.SQL("SELECT owner_epoch FROM {} WHERE scope = %s").format(
                    self._scoped_writers_table
                ), (writer_scope,),
            ).fetchone()
            current = -1 if row is None else int(row[0])
            if current != owner_epoch:
                raise StaleWriterError(
                    f"stale scoped writer epoch {owner_epoch} for {writer_scope}; "
                    f"current epoch is {current}"
                )
            return current
        self._connection.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
            (self.namespace,),
        )
        owner_row = self._connection.execute(
            sql.SQL("SELECT owner_epoch FROM {} WHERE singleton = 1").format(
                self._meta_table
            )
        ).fetchone()
        current_epoch = -1 if owner_row is None else int(owner_row[0])
        if current_epoch > 0 and owner_epoch is None:
            raise StaleWriterError(
                f"writer fencing is active at epoch {current_epoch}; "
                "owner_epoch is required"
            )
        if owner_epoch is not None and current_epoch != owner_epoch:
            raise StaleWriterError(
                f"stale writer epoch {owner_epoch}; current epoch is {current_epoch}"
            )
        return current_epoch

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

    def _fresh(self) -> None:
        self._refresh_from_db()

    def job_state(self, job_id: str):
        self._fresh()
        return super().job_state(job_id)

    def domain_delivery(self, mutation_id: str):
        self._fresh()
        return super().domain_delivery(mutation_id)

    def domain_deliveries(self):
        self._fresh()
        return super().domain_deliveries()

    def sink_delivery(self, delivery_id: str):
        self._fresh()
        return super().sink_delivery(delivery_id)

    def sink_deliveries(self, *, job_id=None, sink_name=None):
        self._fresh()
        return super().sink_deliveries(job_id=job_id, sink_name=sink_name)

    def sink_checkpoint(self, job_id: str, sink_name: str):
        self._fresh()
        return super().sink_checkpoint(job_id, sink_name)

    def sink_checkpoints(self, *, job_id=None):
        self._fresh()
        return super().sink_checkpoints(job_id=job_id)

    def business_effect(self, effect_id: str):
        self._fresh()
        return super().business_effect(effect_id)

    def business_effects(self):
        self._fresh()
        return super().business_effects()

    def job_states(self):
        self._fresh()
        return super().job_states()

    def committed_tick(self, *, owner_id: str) -> int:
        self._fresh()
        return super().committed_tick(owner_id=owner_id)

    def events(self):
        self._fresh()
        return super().events()

    def entity(self, entity_type: str, entity_id: str):
        self._fresh()
        return super().entity(entity_type, entity_id)

    def command(self, command_id: str):
        self._fresh()
        return super().command(command_id)

    def scheduled_work(self):
        self._fresh()
        return super().scheduled_work()

    def due_scheduled_work(self, at):
        self._fresh()
        return tuple(
            work for work in super().scheduled_work()
            if work.due_at <= at
        )

    def simulation_position(self, *, owner_id: str):
        self._fresh()
        return super().simulation_position(owner_id=owner_id)

    def scenario_state(self, *, owner_id: str):
        self._fresh()
        return super().scenario_state(owner_id=owner_id)

    def resource_definitions(self):
        self._fresh()
        return super().resource_definitions()

    def resource_demands(self):
        self._fresh()
        return super().resource_demands()

    def resource_reservations(self):
        self._fresh()
        return super().resource_reservations()

    def resource_release_intents(self):
        self._fresh()
        return super().resource_release_intents()

    def store_definitions(self):
        self._fresh()
        return super().store_definitions()

    def store_items(self):
        self._fresh()
        return super().store_items()

    def store_put_intents(self):
        self._fresh()
        return super().store_put_intents()

    def store_get_requests(self):
        self._fresh()
        return super().store_get_requests()

    def store_get_results(self):
        self._fresh()
        return super().store_get_results()

    def container_definitions(self):
        self._fresh()
        return super().container_definitions()

    def container_states(self):
        self._fresh()
        return super().container_states()

    def container_operation_intents(self):
        self._fresh()
        return super().container_operation_intents()

    def container_operation_results(self):
        self._fresh()
        return super().container_operation_results()

    def preemptive_resource_definitions(self):
        self._fresh()
        return super().preemptive_resource_definitions()

    def preemptive_resource_demands(self):
        self._fresh()
        return super().preemptive_resource_demands()

    def preemptive_resource_reservations(self):
        self._fresh()
        return super().preemptive_resource_reservations()

    def preemptive_resource_release_intents(self):
        self._fresh()
        return super().preemptive_resource_release_intents()

    def resource_preemption_results(self):
        self._fresh()
        return super().resource_preemption_results()
