"""Idempotent DDL bootstrap of the temporal resource ledger tables.

Split from ``postgres_resource_ledger.py`` (``PostgresTemporalResourceLedger._bootstrap``).
"""
from __future__ import annotations

from psycopg import sql


def bootstrap_schema(
    db, store, *, pools: sql.Identifier, reservations: sql.Identifier,
    outages: sql.Identifier, events: sql.Identifier,
) -> None:
    """Create the ledger tables and indexes of ``store.namespace`` in one transaction."""
    with db.transaction():
        # DDL-only bootstrap is serialized per namespace (not per action).
        db.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (store.namespace,),
        )
        db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    pool_key TEXT PRIMARY KEY, definition TEXT NOT NULL
                )
            """).format(pools))
        db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    reservation_id TEXT PRIMARY KEY,
                    pool_key TEXT NOT NULL REFERENCES {}(pool_key),
                    instance_id TEXT,
                    owner_id TEXT NOT NULL,
                    start_at TIMESTAMPTZ NOT NULL,
                    end_at TIMESTAMPTZ NOT NULL,
                    units INTEGER NOT NULL CHECK (units > 0),
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('reserved','released','preempted','failed')
                    ),
                    stopped_at TIMESTAMPTZ,
                    causation_id TEXT,
                    CHECK (start_at < end_at),
                    CHECK (stopped_at IS NULL OR
                           (stopped_at >= start_at AND stopped_at <= end_at))
                )
            """).format(reservations, pools))
        db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS {} ON {} (pool_key, start_at, end_at)
            """).format(
            sql.Identifier(f"{store.namespace}_resource_booking_interval_idx"),
            reservations,
        ))
        # GiST's native timestamptz-range overlap operator constrains
        # BOTH endpoints of a bounded query, including long past and
        # arbitrarily distant future intervals. No btree_gist extension.
        # [start, effective_end) encodes released/preempted bookings and
        # an instantaneous stop as an empty range. Namespace-safe names.
        db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS {} ON {} USING gist
                    (tstzrange(start_at, COALESCE(stopped_at, end_at), '[)'))
            """).format(
            sql.Identifier(f"{store.namespace}_rb_window_gist"),
            reservations,
        ))
        db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    outage_id TEXT PRIMARY KEY,
                    pool_key TEXT NOT NULL REFERENCES {}(pool_key),
                    instance_id TEXT,
                    start_at TIMESTAMPTZ NOT NULL,
                    end_at TIMESTAMPTZ NOT NULL,
                    recovered_at TIMESTAMPTZ,
                    CHECK (start_at < end_at),
                    CHECK (recovered_at IS NULL OR
                           (recovered_at >= start_at AND recovered_at <= end_at))
                )
            """).format(outages, pools))
        db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS {} ON {} USING gist
                    (tstzrange(start_at, COALESCE(recovered_at, end_at), '[)'))
            """).format(
            sql.Identifier(f"{store.namespace}_ro_window_gist"),
            outages,
        ))
        # Temporal GiST alone scans simultaneous outages for EVERY
        # unrelated pool. An independent pool-key B-tree lets the planner
        # select or bitmap-AND a single pool even when many others occupy
        # the same time window. Bookings already have a pool-key-leading
        # B-tree; this closes the missing outage access path.
        db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS {} ON {} (pool_key)
            """).format(
            sql.Identifier(f"{store.namespace}_ro_pool_idx"),
            outages,
        ))
        db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    event_id TEXT PRIMARY KEY,
                    pool_key TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    occurred_at TIMESTAMPTZ NOT NULL,
                    causation_event_id TEXT
                )
            """).format(events))
