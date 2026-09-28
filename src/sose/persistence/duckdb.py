from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Iterator

import duckdb

from .memory import MemoryPersistence, MemoryUnitOfWork, _State, fork_state
from .records import StateRecord, changes_for_dirty_records, records_to_state


_SCHEMA_VERSION = 1


class DuckDBPersistence(MemoryPersistence):
    """DuckDB-backed record-level persistence using the shared SOSE delta model."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        super().__init__()
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        self._connection = duckdb.connect(self.path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sose_record_meta (
                singleton INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sose_record (
                collection VARCHAR NOT NULL,
                record_key VARCHAR NOT NULL,
                position BIGINT NOT NULL,
                payload VARCHAR NOT NULL,
                PRIMARY KEY (collection, record_key)
            )
            """
        )
        row = self._connection.execute(
            "SELECT schema_version FROM sose_record_meta WHERE singleton = 1"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO sose_record_meta VALUES (1, ?)",
                [_SCHEMA_VERSION],
            )
        elif int(row[0]) != _SCHEMA_VERSION:
            raise RuntimeError(
                "unsupported DuckDBPersistence schema version: "
                f"{row[0]}"
            )
        self._refresh_from_db()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "DuckDBPersistence":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _refresh_from_db(self) -> None:
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
                    """
                    DELETE FROM sose_record
                    WHERE collection = ? AND record_key = ?
                    """,
                    [change.collection, change.key],
                )
            elif change.operation == "upsert":
                assert change.position is not None
                assert change.payload is not None
                self._connection.execute(
                    """
                    DELETE FROM sose_record
                    WHERE collection = ? AND record_key = ?
                    """,
                    [change.collection, change.key],
                )
                self._connection.execute(
                    """
                    INSERT INTO sose_record(
                        collection,
                        record_key,
                        position,
                        payload
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        change.collection,
                        change.key,
                        change.position,
                        change.payload,
                    ],
                )
            else:  # pragma: no cover
                raise RuntimeError(
                    f"unknown state record operation: {change.operation}"
                )
        return len(changes)

    @contextmanager
    def transaction(self) -> Iterator[MemoryUnitOfWork]:
        self._connection.execute("BEGIN TRANSACTION")
        self._refresh_from_db()
        before = self._state
        try:
            uow = MemoryUnitOfWork(fork_state(self._state), self)
            yield uow
            if not uow._closed:
                uow.commit()
            self._apply_changes(
                before,
                self._state,
                uow.dirty_records,
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
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
