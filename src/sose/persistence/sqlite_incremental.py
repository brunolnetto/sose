from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
import re
import sqlite3
from time import monotonic, sleep
from typing import Iterator

from .memory import MemoryPersistence, MemoryUnitOfWork, _State, fork_state
from .records import StateRecord, changes_for_dirty_records, records_to_state


_SCHEMA_VERSION = 3
_NAMESPACE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,39}$")


@dataclass(frozen=True, slots=True)
class WriterLease:
    owner_id: str
    epoch: int


class StaleWriterError(RuntimeError):
    """Raised when a writer attempts to commit with an obsolete fencing epoch."""



class SQLiteIncrementalPersistence(MemoryPersistence):
    """SQLite adapter that persists only changed durable records per transaction."""

    def __init__(self, path: str | Path, *, namespace: str = "sose") -> None:
        super().__init__()
        self._validate_namespace(namespace)
        self.namespace = namespace
        self._meta_table = f'"{namespace}_record_meta"'
        self._record_table = f'"{namespace}_record"'
        self.path = str(path)
        self._ensure_parent_path()
        self._connection = self._connect()
        self._closed = False
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._bootstrap_schema()
        self._ensure_wal()
        self._revision = -1
        self._refresh_from_db(force=True)

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if _NAMESPACE_RE.fullmatch(namespace):
            return
        raise ValueError(
            "SQLiteIncrementalPersistence namespace must be a lowercase SQL-safe "
            "identifier with at most 40 characters"
        )

    def _ensure_parent_path(self) -> None:
        if self.path == ":memory:":
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self):
        return sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=30.0,
            check_same_thread=False,
        )

    def _bootstrap_schema(self) -> None:
        # Schema creation, singleton bootstrap, and migrations share one
        # BEGIN IMMEDIATE lock. Concurrent constructors therefore serialize
        # before any metadata is inspected or mutated.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._create_tables()
            columns = self._meta_columns()
            version = self._meta_schema_version()
            self._migrate_schema(version, columns)

            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _create_tables(self) -> None:
        self._connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._meta_table} (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                schema_version INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                owner_id TEXT,
                owner_epoch INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._record_table} (
                collection TEXT NOT NULL,
                record_key TEXT NOT NULL,
                position INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (collection, record_key)
            )
            """
        )

    def _meta_columns(self) -> set[str]:
        return {
            str(row[1])
            for row in self._connection.execute(
                f"PRAGMA table_info({self._meta_table})"
            ).fetchall()
        }

    def _meta_schema_version(self) -> int:
        row = self._connection.execute(
            f"SELECT schema_version FROM {self._meta_table} WHERE singleton = 1"
        ).fetchone()
        if row is None:
            self._connection.execute(
                f"""
                INSERT OR IGNORE INTO {self._meta_table}(
                    singleton,
                    schema_version,
                    revision
                )
                VALUES (1, ?, 0)
                """,
                (_SCHEMA_VERSION,),
            )
            row = self._connection.execute(
                f"""
                SELECT schema_version
                FROM {self._meta_table}
                WHERE singleton = 1
                """
            ).fetchone()
        if row is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("SQLite incremental metadata bootstrap failed")
        return int(row[0])

    def _migrate_schema(self, version: int, columns: set[str]) -> None:
        if version == 1:
            if "revision" not in columns:
                self._connection.execute(
                    f"""
                    ALTER TABLE {self._meta_table}
                    ADD COLUMN revision INTEGER NOT NULL DEFAULT 0
                    """
                )
            version = 2
        if version == 2:
            if "owner_id" not in columns:
                self._connection.execute(
                    f"ALTER TABLE {self._meta_table} ADD COLUMN owner_id TEXT"
                )
            if "owner_epoch" not in columns:
                self._connection.execute(
                    f"""
                    ALTER TABLE {self._meta_table}
                    ADD COLUMN owner_epoch INTEGER NOT NULL DEFAULT 0
                    """
                )
            self._connection.execute(
                f"""
                UPDATE {self._meta_table}
                SET schema_version = 3
                WHERE singleton = 1
                """
            )
            return
        if version != _SCHEMA_VERSION:
            raise RuntimeError(
                "unsupported SQLiteIncrementalPersistence schema version: "
                f"{version}"
            )

    def _ensure_wal(self) -> None:
        """Enable WAL safely when multiple constructors race on one database."""

        deadline = monotonic() + 30.0
        while True:
            try:
                row = self._connection.execute(
                    "PRAGMA journal_mode = WAL"
                ).fetchone()
                mode = "" if row is None else str(row[0]).lower()
                if mode != "wal":
                    raise RuntimeError(
                        "SQLiteIncrementalPersistence requires WAL journal mode"
                    )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or monotonic() >= deadline:
                    raise
                sleep(0.01)

    def close(self) -> None:
        if self._closed:
            return
        conn = getattr(self, "_connection", None)
        self._connection = None
        self._closed = True
        if conn is not None:
            conn.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "SQLiteIncrementalPersistence":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _database_revision(self) -> int:
        row = self._connection.execute(
            f"SELECT revision FROM {self._meta_table} WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def _refresh_from_db(self, *, force: bool = False) -> None:
        revision = self._database_revision()
        if not force and revision == self._revision:
            return

        rows = self._connection.execute(
            f"""
            SELECT collection, record_key, position, payload
            FROM {self._record_table}
            ORDER BY collection, position, record_key
            """
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
                    f"DELETE FROM {self._record_table} WHERE collection = ? AND record_key = ?",
                    (change.collection, change.key),
                )
            elif change.operation == "upsert":
                assert change.position is not None
                assert change.payload is not None
                self._connection.execute(
                    f"""
                    INSERT INTO {self._record_table}(collection, record_key, position, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(collection, record_key) DO UPDATE SET
                        position = excluded.position,
                        payload = excluded.payload
                    """,
                    (
                        change.collection,
                        change.key,
                        change.position,
                        change.payload,
                    ),
                )
            else:  # pragma: no cover - defensive guard for internal enum-by-string
                raise RuntimeError(
                    f"unknown state record operation: {change.operation}"
                )
        return len(changes)

    def writer_epoch(self) -> int:
        row = self._connection.execute(
            f"SELECT owner_epoch FROM {self._meta_table} WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("writer metadata disappeared")
        return int(row[0])

    def claim_writer(self, owner_id: str, *, expected_epoch: int) -> WriterLease:
        """Atomically acquire the next fencing epoch using compare-and-swap."""

        if not owner_id:
            raise ValueError("owner_id cannot be empty")
        if expected_epoch < 0:
            raise ValueError("expected_epoch must be >= 0")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(
                f"""
                UPDATE {self._meta_table}
                SET owner_id = ?, owner_epoch = owner_epoch + 1
                WHERE singleton = 1 AND owner_epoch = ?
                """,
                (owner_id, expected_epoch),
            )
            if cursor.rowcount != 1:
                raise StaleWriterError(
                    f"writer claim lost: expected epoch {expected_epoch}"
                )
            row = self._connection.execute(
                f"SELECT owner_epoch FROM {self._meta_table} WHERE singleton = 1"
            ).fetchone()
            if row is None:  # pragma: no cover - writer metadata invariant
                raise RuntimeError("writer metadata disappeared")
            self._connection.commit()
            return WriterLease(owner_id=owner_id, epoch=int(row[0]))
        except Exception:
            self._connection.rollback()
            raise

    @contextmanager
    def transaction(
        self,
        *,
        owner_epoch: int | None = None,
    ) -> Iterator[MemoryUnitOfWork]:
        self._connection.execute("BEGIN IMMEDIATE")
        row = self._connection.execute(
            f"SELECT owner_epoch FROM {self._meta_table} WHERE singleton = 1"
        ).fetchone()
        current_epoch = -1 if row is None else int(row[0])
        if current_epoch > 0 and owner_epoch is None:
            self._connection.rollback()
            raise StaleWriterError(
                f"writer fencing is active at epoch {current_epoch}; owner_epoch is required"
            )
        if owner_epoch is not None and current_epoch != owner_epoch:
            self._connection.rollback()
            raise StaleWriterError(
                f"stale writer epoch {owner_epoch}; current epoch is {current_epoch}"
            )
        self._refresh_from_db()
        before = self._state
        try:
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
                next_revision = self._revision + 1
                self._connection.execute(
                    f"""
                    UPDATE {self._meta_table}
                    SET revision = ?
                    WHERE singleton = 1
                    """,
                    (next_revision,),
                )
            else:
                next_revision = self._revision
            self._connection.commit()
            self._revision = next_revision
        except Exception:
            self._connection.rollback()
            self._state = before
            raise

    def persisted_record_count(self) -> int:
        row = self._connection.execute(
            f"SELECT COUNT(*) FROM {self._record_table}"
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
