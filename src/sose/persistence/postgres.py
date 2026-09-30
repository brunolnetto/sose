from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import re
from typing import Iterator

import psycopg
from psycopg import sql

from .memory import MemoryPersistence, MemoryUnitOfWork, _State, fork_state
from .records import StateRecord, changes_for_dirty_records, records_to_state


_SCHEMA_VERSION = 2

@dataclass(frozen=True, slots=True)
class WriterLease:
    owner_id: str
    epoch: int


class StaleWriterError(RuntimeError):
    """Raised when a PostgreSQL writer loses its fencing epoch."""

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
        self._connection = psycopg.connect(dsn, autocommit=True)

        with self._connection.transaction():
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
        for change in changes:
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
            else:  # pragma: no cover
                raise RuntimeError(
                    f"unknown state record operation: {change.operation}"
                )
        return len(changes)

    def claim_writer(self, owner_id: str, *, expected_epoch: int) -> WriterLease:
        if not owner_id:
            raise ValueError("owner_id cannot be empty")
        if expected_epoch < 0:
            raise ValueError("expected_epoch must be >= 0")
        with self._connection.transaction():
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

    @contextmanager
    def transaction(
        self,
        *,
        owner_epoch: int | None = None,
    ) -> Iterator[MemoryUnitOfWork]:
        before: _State | None = None
        try:
            with self._connection.transaction():
                owner_row = self._connection.execute(
                    sql.SQL(
                        "SELECT owner_epoch FROM {} WHERE singleton = 1 FOR UPDATE"
                    ).format(self._meta_table)
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
                # Force a transaction-consistent refresh. Writers may overlap,
                # and dirty-record persistence prevents unrelated updates from
                # replacing one another.
                self._refresh_from_db(force=True)
                before = self._state
                uow = MemoryUnitOfWork(fork_state(self._state), self)
                yield uow
                if not uow._closed:
                    uow.commit()

                changed = self._apply_changes(
                    before,
                    self._state,
                    uow.dirty_records,
                )
                if changed:
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
                    if row is None:  # pragma: no cover
                        raise RuntimeError(
                            "PostgresPersistence revision update failed"
                        )
                    next_revision = int(row[0])
                else:
                    next_revision = self._revision
            # A concurrent writer may have committed records that were not
            # present in this transaction's in-memory snapshot before our
            # revision increment. Never claim the local cache represents the
            # returned global revision; force the next read to refresh.
            self._revision = -1 if changed else next_revision
        except Exception:
            if before is not None:
                self._state = before
            raise

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

    def job_states(self):
        self._fresh()
        return super().job_states()

    def committed_tick(self) -> int:
        self._fresh()
        return super().committed_tick()

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

    def simulation_position(self):
        self._fresh()
        return super().simulation_position()

    def scenario_state(self):
        self._fresh()
        return super().scenario_state()

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
