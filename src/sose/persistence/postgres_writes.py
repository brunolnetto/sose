"""Dirty-record and event-log write path of PostgresPersistence (split from postgres.py)."""

from __future__ import annotations

import psycopg
from psycopg import sql

from .codec import dumps, loads
from .memory import _State
from .postgres_leases import StaleWriterError
from .records import changes_for_dirty_records


class PostgresRecordWritesMixin:
    """Persist one unit of work's dirty records inside the caller's SQL transaction."""

    namespace: str
    _connection: psycopg.Connection
    _record_table: sql.Identifier

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
            if change.collection == "simulation_positions" and change.operation == "upsert":
                self._replace_simulation_position(before, change)
                continue
            self._write_record_change(change)
        if event_appends:
            self._append_postgres_events(before, after, event_appends)
        return len(changes)

    def _replace_simulation_position(self, before: _State, change) -> None:
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
                    change.position,
                    change.payload,
                    change.collection,
                    change.key,
                    dumps(expected),
                ),
            )
        if result.fetchone() is None:
            raise StaleWriterError(f"stale simulation position for owner {owner_id!r}")

    def _write_record_change(self, change) -> None:
        if change.operation == "delete":
            self._connection.execute(
                sql.SQL("DELETE FROM {} WHERE collection = %s AND record_key = %s").format(
                    self._record_table
                ),
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
            raise RuntimeError(f"unknown state record operation: {change.operation}")

    def _append_postgres_events(
        self,
        before: _State,
        after: _State,
        changes,
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
            sql.SQL("SELECT COALESCE(MAX(position), -1) FROM {} WHERE collection = %s").format(
                self._record_table
            ),
            ("events",),
        ).fetchone()
        assert row is not None
        next_position = int(row[0]) + 1
        for change in sorted(changes, key=lambda item: item.position):
            event = _appended_event(before, after, change)
            if _replayed_in_snapshot(before, event):
                continue
            if self._insert_event(event, next_position):
                next_position += 1

    def _insert_event(self, event, position: int) -> bool:
        """Store one event at ``position``; False for an exact committed replay."""
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
            ("events", key, position, payload),
        ).fetchone()
        if stored is None:
            original = self._connection.execute(
                sql.SQL("SELECT payload FROM {} WHERE collection = %s AND record_key = %s").format(
                    self._record_table
                ),
                ("events", key),
            ).fetchone()
            if original is None or loads(str(original[0])) != event:
                raise ValueError(f"conflicting PostgreSQL durable event ID: {event.event_id}")
            return False
        return True


def _appended_event(before: _State, after: _State, change):
    """Return the event one append change refers to, rejecting non-append changes."""
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
    return event


def _replayed_in_snapshot(before: _State, event) -> bool:
    """Tell whether ``event`` exactly replays one already in this UoW's snapshot."""
    # Exact deterministic replay is idempotent; a reused identity
    # with changed causal facts is not. Inspect legacy numeric-key
    # events in this UoW's snapshot as well as new identity keys.
    earlier = tuple(item for item in before.events if item.event_id == event.event_id)
    if earlier:
        if any(item != event for item in earlier):
            raise ValueError(f"conflicting PostgreSQL durable event ID: {event.event_id}")
        return True
    return False
