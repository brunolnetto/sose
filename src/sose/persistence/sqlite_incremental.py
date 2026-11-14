from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
from time import monotonic, sleep
from typing import Iterator

from .memory import (
    tracked_transaction_depth,
    MemoryPersistence,
    MemoryUnitOfWork,
    _State,
    fork_state,
)
from .records import StateRecord, changes_for_dirty_records, records_to_state
from .sqlite_incremental_reads import SQLiteIncrementalFreshReadsMixin
from .sqlite_incremental_schema import (
    _SCHEMA_VERSION,  # noqa: F401 - re-exported for compatibility
    SQLiteIncrementalSchemaMixin,
)


_NAMESPACE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,39}$")


@dataclass(frozen=True, slots=True)
class WriterLease:
    owner_id: str
    epoch: int


class StaleWriterError(RuntimeError):
    """Raised when a writer attempts to commit with an obsolete fencing epoch."""


class SQLiteIncrementalPersistence(
    SQLiteIncrementalSchemaMixin,
    SQLiteIncrementalFreshReadsMixin,
    MemoryPersistence,
):
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

    def _ensure_wal(self) -> None:
        """Enable WAL safely when multiple constructors race on one database."""

        deadline = monotonic() + 30.0
        while True:
            try:
                row = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
                mode = "" if row is None else str(row[0]).lower()
                if mode != "wal":
                    raise RuntimeError("SQLiteIncrementalPersistence requires WAL journal mode")
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

    def _load_state_from_db(self, *, force: bool = False) -> tuple[_State, int]:
        revision = self._database_revision()
        if not force and revision == self._revision:
            return self._state, revision

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
        return records_to_state(records), revision

    def _refresh_from_db(self, *, force: bool = False) -> None:
        self._state, self._revision = self._load_state_from_db(force=force)

    def _snapshot_state(self) -> _State:
        self._connection.execute("BEGIN")
        try:
            state, _ = self._load_state_from_db()
            return state
        finally:
            self._connection.rollback()

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
                raise RuntimeError(f"unknown state record operation: {change.operation}")
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
                raise StaleWriterError(f"writer claim lost: expected epoch {expected_epoch}")
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

    def claim_writer_and_register(
        self,
        owner_id: str,
        *,
        expected_epoch: int,
        initial_job,
    ) -> WriterLease:
        """One BEGIN IMMEDIATE commits the global fencing epoch and job ID.

        Unlike separate claim_writer()/transaction() commits, a real worker
        crash immediately after this method cannot orphan a writer epoch.
        """
        if not owner_id:
            raise ValueError("owner_id cannot be empty")
        if expected_epoch < 0:
            raise ValueError("expected_epoch must be >= 0")

        self._connection.execute("BEGIN IMMEDIATE")
        before = None
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
                raise StaleWriterError(f"writer claim lost: expected epoch {expected_epoch}")
            row = self._connection.execute(
                f"SELECT owner_epoch FROM {self._meta_table} WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("writer metadata disappeared")
            epoch = int(row[0])
            self._refresh_from_db()
            before = self._state
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
            self._connection.commit()
            self._revision = next_revision
            return WriterLease(owner_id=owner_id, epoch=epoch)
        except Exception:
            self._connection.rollback()
            if before is not None:
                self._state = before
            raise

    def boundary_protocol_snapshot(self):
        """Fence-neutral, coherent read across SQLite incremental records.

        Never claim an epoch, begin an immediate transaction, or persist the
        forked working state. Read metadata revision and all records under the
        same deferred SQLite snapshot, including after another writer commits.
        """
        before, revision = self._state, self._revision
        self._connection.execute("BEGIN")
        try:
            self._refresh_from_db(force=True)
            return super().boundary_protocol_snapshot()
        finally:
            self._connection.rollback()
            self._state, self._revision = before, revision

    def _require_current_writer_epoch(self, owner_epoch: int | None) -> None:
        """Roll back the open transaction and raise unless ``owner_epoch`` is current."""
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

    @tracked_transaction_depth
    @contextmanager
    def transaction(
        self,
        *,
        owner_epoch: int | None = None,
        owner_id: str | None = None,
    ) -> Iterator[MemoryUnitOfWork]:
        if owner_id is not None and not owner_id:
            raise ValueError("owner_id must not be empty")
        self._connection.execute("BEGIN IMMEDIATE")
        self._require_current_writer_epoch(owner_epoch)
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
        row = self._connection.execute(f"SELECT COUNT(*) FROM {self._record_table}").fetchone()
        return 0 if row is None else int(row[0])

    def _fresh(self) -> None:
        self._refresh_from_db()
