"""Row readers and canonical snapshot of the PostgreSQL temporal resource ledger.

Split from ``postgres_resource_ledger.py``: the read-only methods of
``PostgresTemporalResourceLedger``, which inherits them from this mixin unchanged.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json

from psycopg import sql

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import TemporalOutage, TemporalReservation


class LedgerReadsMixin:
    """Queries over the ledger tables; the concrete ledger provides the attributes."""

    _db: object
    _pools: sql.Identifier
    _reservations: sql.Identifier
    _outages: sql.Identifier
    _events: sql.Identifier

    def _pool(self, key: str) -> ResourcePoolContract:
        row = self._db.execute(
            sql.SQL("SELECT definition FROM {} WHERE pool_key = %s").format(self._pools),
            (key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"resource pool not registered: {key}")
        data = json.loads(row[0])
        data["instance_ids"] = tuple(data["instance_ids"])
        return ResourcePoolContract(**data)

    def _booking(self, row, *, pool: ResourcePoolContract | None = None) -> TemporalReservation:
        pool = pool or self._pool(row[1])
        return TemporalReservation(
            reservation_id=row[0], address=pool.address(instance_id=row[2]),
            owner_id=row[3], start_at=row[4], end_at=row[5],
            units=row[6], priority=row[7], status=row[8],
            stopped_at=row[9], causation_id=row[10],
        )

    def _get_booking(self, reservation_id: str) -> TemporalReservation | None:
        row = self._db.execute(sql.SQL("""
            SELECT reservation_id, pool_key, instance_id, owner_id, start_at,
                   end_at, units, priority, status, stopped_at, causation_id
            FROM {} WHERE reservation_id = %s
        """).format(self._reservations), (reservation_id,)).fetchone()
        return self._booking(row) if row else None

    def _bookings(self, key: str) -> tuple[TemporalReservation, ...]:
        rows = self._db.execute(sql.SQL("""
            SELECT reservation_id, pool_key, instance_id, owner_id, start_at,
                   end_at, units, priority, status, stopped_at, causation_id
            FROM {} WHERE pool_key = %s ORDER BY reservation_id
        """).format(self._reservations), (key,)).fetchall()
        return tuple(self._booking(row) for row in rows)

    def _outage(self, row, *, pool: ResourcePoolContract | None = None) -> TemporalOutage:
        pool = pool or self._pool(row[1])
        return TemporalOutage(
            outage_id=row[0], address=pool.address(instance_id=row[2]),
            start_at=row[3], end_at=row[4], recovered_at=row[5],
        )

    def _outages_for(self, key: str) -> tuple[TemporalOutage, ...]:
        rows = self._db.execute(sql.SQL("""
            SELECT outage_id, pool_key, instance_id, start_at,
                   end_at, recovered_at
            FROM {} WHERE pool_key = %s ORDER BY outage_id
        """).format(self._outages), (key,)).fetchall()
        return tuple(self._outage(row) for row in rows)

    def _window_bookings(
        self, key: str, pool: ResourcePoolContract, *, start: datetime,
        end: datetime,
    ) -> tuple[TemporalReservation, ...]:
        """Read only booking intervals overlapping the finite advice horizon."""
        rows = self._db.execute(sql.SQL("""
            SELECT reservation_id, pool_key, instance_id, owner_id, start_at,
                   end_at, units, priority, status, stopped_at, causation_id
            FROM {} WHERE pool_key = %s
              AND tstzrange(start_at, COALESCE(stopped_at, end_at), '[)')
                  && tstzrange(%s, %s, '[)')
            ORDER BY reservation_id
        """).format(self._reservations), (key, start, end)).fetchall()
        return tuple(self._booking(row, pool=pool) for row in rows)

    def _window_outages(
        self, key: str, pool: ResourcePoolContract, *, start: datetime,
        end: datetime,
    ) -> tuple[TemporalOutage, ...]:
        """Ignore historical outages that cannot constrain any candidate."""
        rows = self._db.execute(sql.SQL("""
            SELECT outage_id, pool_key, instance_id, start_at, end_at, recovered_at
            FROM {} WHERE pool_key = %s
              AND tstzrange(start_at, COALESCE(recovered_at, end_at), '[)')
                  && tstzrange(%s, %s, '[)')
            ORDER BY outage_id
        """).format(self._outages), (key, start, end)).fetchall()
        return tuple(self._outage(row, pool=pool) for row in rows)

    def get(self, reservation_id: str) -> TemporalReservation | None:
        return self._get_booking(reservation_id)

    def reservations(self) -> tuple[TemporalReservation, ...]:
        rows = self._db.execute(sql.SQL("""
            SELECT reservation_id, pool_key, instance_id, owner_id, start_at,
                   end_at, units, priority, status, stopped_at, causation_id
            FROM {} ORDER BY reservation_id
        """).format(self._reservations)).fetchall()
        return tuple(self._booking(row) for row in rows)

    def outages(self) -> tuple[TemporalOutage, ...]:
        rows = self._db.execute(sql.SQL("""
            SELECT outage_id, pool_key, instance_id, start_at,
                   end_at, recovered_at
            FROM {} ORDER BY outage_id
        """).format(self._outages)).fetchall()
        return tuple(self._outage(row) for row in rows)

    def events(self) -> tuple[tuple, ...]:
        return tuple(self._db.execute(sql.SQL("""
            SELECT event_id, pool_key, subject_id, action, occurred_at,
                   causation_event_id FROM {} ORDER BY event_id
        """).format(self._events)).fetchall())

    def snapshot(self) -> dict:
        """Canonical, worker-independent business state and causal ledger."""
        pools = self._db.execute(sql.SQL("""
            SELECT pool_key, definition FROM {} ORDER BY pool_key
        """).format(self._pools)).fetchall()
        def normalize(values):
            return sorted(
                [json.dumps(asdict(item), sort_keys=True, default=str,
                            separators=(",", ":")) for item in values]
            )
        return {
            "pools": [(key, json.loads(value)) for key, value in pools],
            "reservations": normalize(self.reservations()),
            "outages": normalize(self.outages()),
            "events": sorted(
                [tuple(x.isoformat() if isinstance(x, datetime) else x for x in row)
                 for row in self.events()]
            ),
        }
