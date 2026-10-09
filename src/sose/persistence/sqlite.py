from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3
from typing import Iterator

from .codec import dumps, loads
from .memory import MemoryPersistence, MemoryUnitOfWork, _State, fork_state
from .sqlite_migrations import (
    CURRENT_CODEC_VERSION,
    CURRENT_SCHEMA_VERSION,
    SQLiteSchemaInfo,
    ensure_schema,
    read_schema_info,
)


class SQLitePersistence(MemoryPersistence):
    """SQLite-backed SOSE persistence with the Memory semantic contract.

    v0.8 deliberately stores one tagged-JSON semantic snapshot per database.
    This prioritizes transactional/restart correctness and schema portability
    over query-level optimization. The public Persistence contract remains
    record-oriented, so a later adapter may normalize tables without changing
    runtime semantics.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=30.0,
            check_same_thread=False,
        )
        self._closed = False
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            ensure_schema(self._connection)
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        self._refresh_from_db()
        if self._connection.execute(
            "SELECT 1 FROM sose_state WHERE singleton = 1"
        ).fetchone() is None:
            self._write_state()
            self._connection.commit()

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

    def schema_info(self) -> SQLiteSchemaInfo:
        """Return the durable SQLite schema/codec versions currently stored."""
        return read_schema_info(self._connection)

    def __enter__(self) -> "SQLitePersistence":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _refresh_from_db(self) -> None:
        row = self._connection.execute(
            "SELECT schema_version, codec_version, payload "
            "FROM sose_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            self._state = _State()
            return
        schema_version, codec_version, payload = row
        if schema_version != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                "SQLitePersistence schema was not migrated to current version: "
                f"{schema_version}"
            )
        if codec_version > CURRENT_CODEC_VERSION:
            raise RuntimeError(
                "SQLitePersistence codec is newer than this runtime: "
                f"database={codec_version}, runtime={CURRENT_CODEC_VERSION}"
            )
        restored = loads(str(payload))
        if not isinstance(restored, _State):
            raise TypeError(
                "SQLitePersistence payload does not contain SOSE durable state"
            )
        self._state = restored

    def _write_state(self) -> None:
        self._connection.execute(
            """
            INSERT INTO sose_state(
                singleton,
                schema_version,
                codec_version,
                payload
            )
            VALUES (1, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                schema_version = excluded.schema_version,
                codec_version = excluded.codec_version,
                payload = excluded.payload
            """,
            (
                CURRENT_SCHEMA_VERSION,
                CURRENT_CODEC_VERSION,
                dumps(self._state),
            ),
        )

    @contextmanager
    def transaction(self) -> Iterator[MemoryUnitOfWork]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._refresh_from_db()
            uow = MemoryUnitOfWork(fork_state(self._state), self)
            yield uow
            if not uow._closed:
                uow.commit()
            self._write_state()
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            self._refresh_from_db()
            raise

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
        return tuple(work for work in super().scheduled_work() if work.due_at <= at)

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
