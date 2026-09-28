from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3
from time import monotonic, sleep
from typing import Iterator

from .memory import MemoryPersistence, MemoryUnitOfWork, _State
from .records import StateRecord, changes_for_dirty_records, records_to_state


_SCHEMA_VERSION = 2


class SQLiteIncrementalPersistence(MemoryPersistence):
    """SQLite adapter that persists only changed durable records per transaction."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=30.0,
        )
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")

        # Schema creation, singleton bootstrap, and migrations share one
        # BEGIN IMMEDIATE lock. Concurrent constructors therefore serialize
        # before any metadata is inspected or mutated.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sose_record_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sose_record (
                    collection TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (collection, record_key)
                )
                """
            )

            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(sose_record_meta)"
                ).fetchall()
            }
            row = self._connection.execute(
                "SELECT schema_version FROM sose_record_meta WHERE singleton = 1"
            ).fetchone()

            if row is None:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO sose_record_meta(
                        singleton,
                        schema_version,
                        revision
                    )
                    VALUES (1, ?, 0)
                    """,
                    (_SCHEMA_VERSION,),
                )
                row = self._connection.execute(
                    """
                    SELECT schema_version
                    FROM sose_record_meta
                    WHERE singleton = 1
                    """
                ).fetchone()

            if row is None:  # pragma: no cover - defensive database invariant
                raise RuntimeError("SQLite incremental metadata bootstrap failed")

            version = int(row[0])
            if version == 1:
                if "revision" not in columns:
                    self._connection.execute(
                        """
                        ALTER TABLE sose_record_meta
                        ADD COLUMN revision INTEGER NOT NULL DEFAULT 0
                        """
                    )
                self._connection.execute(
                    """
                    UPDATE sose_record_meta
                    SET schema_version = 2
                    WHERE singleton = 1
                    """
                )
            elif version != _SCHEMA_VERSION:
                raise RuntimeError(
                    "unsupported SQLiteIncrementalPersistence schema version: "
                    f"{version}"
                )

            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

        self._ensure_wal()
        self._revision = -1
        self._refresh_from_db(force=True)

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
        self._connection.close()

    def __enter__(self) -> "SQLiteIncrementalPersistence":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _database_revision(self) -> int:
        row = self._connection.execute(
            "SELECT revision FROM sose_record_meta WHERE singleton = 1"
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def _refresh_from_db(self, *, force: bool = False) -> None:
        revision = self._database_revision()
        if not force and revision == self._revision:
            return

        rows = self._connection.execute(
            """
            SELECT collection, record_key, position, payload
            FROM sose_record
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
                    "DELETE FROM sose_record WHERE collection = ? AND record_key = ?",
                    (change.collection, change.key),
                )
            elif change.operation == "upsert":
                assert change.position is not None
                assert change.payload is not None
                self._connection.execute(
                    """
                    INSERT INTO sose_record(collection, record_key, position, payload)
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

    @contextmanager
    def transaction(self) -> Iterator[MemoryUnitOfWork]:
        self._connection.execute("BEGIN IMMEDIATE")
        self._refresh_from_db()
        before = deepcopy(self._state)
        try:
            uow = MemoryUnitOfWork(deepcopy(self._state), self)
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
                    """
                    UPDATE sose_record_meta
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
            "SELECT COUNT(*) FROM sose_record"
        ).fetchone()
        return 0 if row is None else int(row[0])

    def _fresh(self) -> None:
        self._refresh_from_db()

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
