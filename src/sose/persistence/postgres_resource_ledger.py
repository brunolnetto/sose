"""PostgreSQL-authoritative temporal resource ledger, additive to EnginePersistence.

All writes for one pool use a pool-scoped PostgreSQL transaction advisory lock.
Independent organizational pools do not take a global runtime mutex. Durable
reservation records and immutable causal events commit in the *same* transaction.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from hashlib import sha256
import json
from typing import Iterable

from psycopg import sql

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import (
    ResourceCapacityError,
    ResourceConflictError,
    TemporalOutage,
    TemporalReservation,
    _aware,
)


class PostgresTemporalResourceLedger:
    """Transactional allocation and recovery, using an existing PG connection.

    This is an additive authoritative subsystem. The generic SOSE snapshot
    serializer and logical SimulationPosition are not modified. A caller must
    not assume the resource ledger and unrelated Engine UoWs commit atomically.
    """

    def __init__(self, persistence) -> None:
        self._store = persistence
        self._db = persistence._connection
        ns = persistence.namespace
        self._pools = sql.Identifier(f"{ns}_resource_pool")
        self._reservations = sql.Identifier(f"{ns}_resource_booking")
        self._outages = sql.Identifier(f"{ns}_resource_outage")
        self._events = sql.Identifier(f"{ns}_resource_event")
        self._bootstrap()

    def _bootstrap(self) -> None:
        with self._db.transaction():
            # DDL-only bootstrap is serialized per namespace (not per action).
            self._db.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (self._store.namespace,),
            )
            self._db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    pool_key TEXT PRIMARY KEY, definition TEXT NOT NULL
                )
            """).format(self._pools))
            self._db.execute(sql.SQL("""
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
            """).format(self._reservations, self._pools))
            self._db.execute(sql.SQL("""
                CREATE INDEX IF NOT EXISTS {} ON {} (pool_key, start_at, end_at)
            """).format(
                sql.Identifier(f"{self._store.namespace}_resource_booking_interval_idx"),
                self._reservations,
            ))
            self._db.execute(sql.SQL("""
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
            """).format(self._outages, self._pools))
            self._db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    event_id TEXT PRIMARY KEY,
                    pool_key TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    occurred_at TIMESTAMPTZ NOT NULL,
                    causation_event_id TEXT
                )
            """).format(self._events))

    def _lock(self, pool_key: str) -> None:
        # No global worker mutex. Hash collision may over-serialize unrelated
        # pools, but must never allow an over-allocation of the same pool.
        self._db.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            (self._store.namespace, pool_key),
        )

    @staticmethod
    def _key(pool: ResourcePoolContract) -> str:
        return pool.address().lock_key()

    def register_pool(self, pool: ResourcePoolContract) -> None:
        """Idempotent immutable contract registration."""
        if not isinstance(pool, ResourcePoolContract):
            raise TypeError("pool must be ResourcePoolContract")
        key = self._key(pool)
        definition = json.dumps(asdict(pool), sort_keys=True, separators=(",", ":"))
        with self._db.transaction():
            self._lock(key)
            row = self._db.execute(
                sql.SQL("SELECT definition FROM {} WHERE pool_key = %s").format(self._pools),
                (key,),
            ).fetchone()
            if row is not None:
                if row[0] != definition:
                    raise ResourceConflictError("immutable resource pool definition changed")
                return
            self._db.execute(
                sql.SQL("INSERT INTO {} (pool_key, definition) VALUES (%s, %s)").format(
                    self._pools
                ), (key, definition),
            )

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

    def _booking(self, row) -> TemporalReservation:
        pool = self._pool(row[1])
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

    def _outage(self, row) -> TemporalOutage:
        pool = self._pool(row[1])
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

    @staticmethod
    def _overlap(a_start, a_end, b_start, b_end) -> bool:
        return a_start < b_end and b_start < a_end

    @classmethod
    def _fits(
        cls, pool: ResourcePoolContract, requested: TemporalReservation,
        others: Iterable[TemporalReservation], outages: Iterable[TemporalOutage],
    ) -> bool:
        # A pool outage forbids all allocation; a named instance outage only
        # forbids that instance. Unavailability is evaluated over the interval.
        for outage in outages:
            if (outage.address.instance_id is None or
                outage.address.instance_id == requested.address.instance_id):
                if cls._overlap(requested.start_at, requested.end_at,
                                outage.start_at, outage.effective_end):
                    return False
        intervals = []
        for item in others:
            if item.effective_end <= item.start_at:
                continue
            if pool.instance_ids and item.address.instance_id != requested.address.instance_id:
                continue
            if cls._overlap(requested.start_at, requested.end_at,
                            item.start_at, item.effective_end):
                intervals.append(item)
        if pool.instance_ids:
            return not intervals
        # Evaluate every occupancy change point, including another booking
        # starting midway through the new reservation (capacity > 1).
        points = {requested.start_at, requested.end_at}
        for item in intervals:
            points.add(max(requested.start_at, item.start_at))
            points.add(min(requested.end_at, item.effective_end))
        timeline = sorted(points)
        for at in timeline[:-1]:
            occupied = requested.units + sum(
                item.units for item in intervals
                if item.start_at <= at < item.effective_end
            )
            if occupied > pool.capacity:
                return False
        return True

    def _event(self, event_id: str, key: str, subject: str, action: str,
               at: datetime, cause: str | None = None) -> None:
        self._db.execute(sql.SQL("""
            INSERT INTO {}(event_id, pool_key, subject_id, action,
                           occurred_at, causation_event_id)
            VALUES (%s,%s,%s,%s,%s,%s)
        """).format(self._events), (event_id, key, subject, action, at, cause))

    def reserve(self, request: TemporalReservation, *, preempt: bool = False) -> TemporalReservation:
        if not isinstance(request, TemporalReservation) or request.status != "reserved":
            raise ValueError("reserve requires a new active TemporalReservation")
        key = request.address.lock_key() if request.address.instance_id is None else replace(
            request.address, instance_id=None
        ).lock_key()
        with self._db.transaction():
            self._lock(key)
            pool = self._pool(key)
            if pool.address() != replace(request.address, instance_id=None):
                raise ResourceConflictError("reservation pool identity mismatch")
            if request.address.instance_id is not None:
                pool.address(instance_id=request.address.instance_id)
            if pool.instance_ids and request.address.instance_id is None:
                raise ValueError("instanced resources must identify the physical instance")
            if not pool.instance_ids and request.address.instance_id is not None:
                raise ValueError("fungible resource pool cannot use physical instance")
            existing = self._get_booking(request.reservation_id)
            if existing is not None:
                if replace(existing, status="reserved", stopped_at=None) != request:
                    raise ResourceConflictError("reservation identity reused with different contents")
                return existing  # Retry after committed write or worker death.
            bookings = list(self._bookings(key))
            outages = self._outages_for(key)
            if not self._fits(pool, request, bookings, outages):
                if not preempt:
                    raise ResourceCapacityError("finite resource unavailable for interval")
                victims = []
                # A higher integer means *lower* priority. Victims are selected
                # deterministically. Never preempt equal or higher priority.
                candidates = sorted(
                    (b for b in bookings if b.priority > request.priority
                     and self._overlap(request.start_at, request.end_at,
                                       b.start_at, b.effective_end)
                     and (not pool.instance_ids or
                          b.address.instance_id == request.address.instance_id)),
                    key=lambda b: (-b.priority, b.reservation_id),
                )
                tentative = list(bookings)
                for candidate in candidates:
                    stop = max(candidate.start_at, request.start_at)
                    proposal = replace(candidate, status="preempted", stopped_at=stop)
                    tentative = [
                        proposal if b.reservation_id == candidate.reservation_id else b
                        for b in tentative
                    ]
                    victims.append(proposal)
                    if self._fits(pool, request, tentative, outages):
                        break
                if not self._fits(pool, request, tentative, outages):
                    raise ResourceCapacityError(
                        "no admissible lower-priority victims or instance unavailable"
                    )
                bookings = tentative
            else:
                victims = []
            self._db.execute(sql.SQL("""
                INSERT INTO {} (reservation_id, pool_key, instance_id, owner_id,
                                start_at, end_at, units, priority,
                                status, stopped_at, causation_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'reserved',NULL,%s)
            """).format(self._reservations), (
                request.reservation_id, key, request.address.instance_id,
                request.owner_id, request.start_at, request.end_at,
                request.units, request.priority, request.causation_id,
            ))
            self._event(f"reserve:{request.reservation_id}", key, request.reservation_id,
                        "reserved", request.start_at)
            for victim in victims:
                self._db.execute(sql.SQL("""
                    UPDATE {} SET status = 'preempted', stopped_at = %s
                    WHERE reservation_id = %s AND status = 'reserved'
                """).format(self._reservations), (victim.stopped_at, victim.reservation_id))
                self._event(
                    f"preempt:{request.reservation_id}:{victim.reservation_id}",
                    key, victim.reservation_id, "preempted", request.start_at,
                    f"reserve:{request.reservation_id}",
                )
            return request

    def get(self, reservation_id: str) -> TemporalReservation | None:
        return self._get_booking(reservation_id)

    def release(self, reservation_id: str, *, at: datetime) -> TemporalReservation:
        _aware(at, "at")
        # Determine key for pool-scoped serialization; immutable reservation IDs
        # must never be moved between pools.
        original = self._get_booking(reservation_id)
        if original is None:
            raise KeyError(reservation_id)
        key = replace(original.address, instance_id=None).lock_key()
        with self._db.transaction():
            self._lock(key)
            current = self._get_booking(reservation_id)
            if current is None:
                raise KeyError(reservation_id)
            if current.status == "released":
                if current.stopped_at != at:
                    raise ResourceConflictError("release retry used a different time")
                return current
            if current.status != "reserved":
                raise ResourceConflictError("reservation already terminated by failure/preemption")
            if not current.start_at <= at <= current.end_at:
                raise ValueError("release must be inside reservation interval")
            self._db.execute(sql.SQL("""
                UPDATE {} SET status='released', stopped_at=%s
                WHERE reservation_id=%s
            """).format(self._reservations), (at, reservation_id))
            self._event(f"release:{reservation_id}", key, reservation_id,
                        "released", at, f"reserve:{reservation_id}")
            return replace(current, status="released", stopped_at=at)

    def fail(self, outage: TemporalOutage) -> TemporalOutage:
        """Persist unavailability and atomically interrupt affected reservations."""
        if not isinstance(outage, TemporalOutage) or outage.recovered_at is not None:
            raise ValueError("fail requires a new TemporalOutage")
        key = replace(outage.address, instance_id=None).lock_key()
        with self._db.transaction():
            self._lock(key)
            pool = self._pool(key)
            if outage.address.instance_id is not None:
                pool.address(instance_id=outage.address.instance_id)
            found = self._db.execute(sql.SQL("""
                SELECT outage_id, pool_key, instance_id, start_at, end_at, recovered_at
                FROM {} WHERE outage_id=%s
            """).format(self._outages), (outage.outage_id,)).fetchone()
            if found:
                prior = self._outage(found)
                if replace(prior, recovered_at=None) != outage:
                    raise ResourceConflictError("outage ID reused with incompatible semantics")
                return prior
            self._db.execute(sql.SQL("""
                INSERT INTO {} (outage_id, pool_key, instance_id, start_at, end_at)
                VALUES (%s,%s,%s,%s,%s)
            """).format(self._outages), (
                outage.outage_id, key, outage.address.instance_id,
                outage.start_at, outage.end_at,
            ))
            self._event(f"outage:{outage.outage_id}", key, outage.outage_id,
                        "failed", outage.start_at)
            for item in self._bookings(key):
                if (outage.address.instance_id is not None and
                    item.address.instance_id != outage.address.instance_id):
                    continue
                if item.status != "reserved" or not self._overlap(
                    item.start_at, item.effective_end,
                    outage.start_at, outage.end_at,
                ):
                    continue
                at = max(item.start_at, outage.start_at)
                self._db.execute(sql.SQL("""
                    UPDATE {} SET status='failed', stopped_at=%s
                    WHERE reservation_id=%s
                """).format(self._reservations), (at, item.reservation_id))
                self._event(
                    f"failed:{outage.outage_id}:{item.reservation_id}",
                    key, item.reservation_id, "failed", at,
                    f"outage:{outage.outage_id}",
                )
            return outage

    def recover(self, outage_id: str, *, at: datetime) -> TemporalOutage:
        _aware(at, "at")
        row = self._db.execute(sql.SQL("""
            SELECT outage_id, pool_key, instance_id, start_at, end_at, recovered_at
            FROM {} WHERE outage_id=%s
        """).format(self._outages), (outage_id,)).fetchone()
        if row is None:
            raise KeyError(outage_id)
        key = row[1]
        with self._db.transaction():
            self._lock(key)
            fresh = self._db.execute(sql.SQL("""
                SELECT outage_id, pool_key, instance_id, start_at, end_at, recovered_at
                FROM {} WHERE outage_id=%s
            """).format(self._outages), (outage_id,)).fetchone()
            outage = self._outage(fresh)
            if outage.recovered_at is not None:
                if outage.recovered_at != at:
                    raise ResourceConflictError("recovery retry used a different time")
                return outage
            if not outage.start_at <= at <= outage.end_at:
                raise ValueError("recovery must lie inside outage interval")
            self._db.execute(sql.SQL("""
                UPDATE {} SET recovered_at=%s WHERE outage_id=%s
            """).format(self._outages), (at, outage_id))
            self._event(f"recover:{outage_id}", key, outage_id,
                        "recovered", at, f"outage:{outage_id}")
            return replace(outage, recovered_at=at)

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

    def audit(self) -> str:
        """Reject overcommit/orphan causal events; return semantic SHA-256.

        Audit should run on a quiesced database or a transactionally consistent
        snapshot; interleaving read-only statements across writers is not proof.
        """
        snapshot = self.snapshot()
        ids = {event[0] for event in snapshot["events"]}
        for event in snapshot["events"]:
            if event[-1] is not None and event[-1] not in ids:
                raise ResourceConflictError("resource event has missing causal predecessor")
        for key, serialized in snapshot["pools"]:
            data = dict(serialized)
            data["instance_ids"] = tuple(data["instance_ids"])
            pool = ResourcePoolContract(**data)
            bookings = [b for b in self.reservations()
                        if replace(b.address, instance_id=None).lock_key() == key]
            outages = [o for o in self.outages()
                       if replace(o.address, instance_id=None).lock_key() == key]
            # Compare the actual occupancy at every boundary; terminal
            # records still occupy the historical prefix until stopped_at.
            boundaries = sorted({
                boundary for b in bookings for boundary in (b.start_at, b.effective_end)
            })
            for start, end in zip(boundaries, boundaries[1:]):
                if start == end:
                    continue
                live = [
                    b for b in bookings if b.start_at <= start < b.effective_end
                ]
                if pool.instance_ids:
                    seen = [b.address.instance_id for b in live]
                    if len(set(seen)) != len(seen):
                        raise ResourceConflictError("same physical instance double-allocated")
                elif sum(b.units for b in live) > pool.capacity:
                    raise ResourceConflictError("historical capacity overcommit")
                for o in outages:
                    if not self._overlap(start, end, o.start_at, o.effective_end):
                        continue
                    for b in live:
                        if o.address.instance_id is None or (
                            o.address.instance_id == b.address.instance_id
                        ):
                            raise ResourceConflictError("allocation overlaps resource outage")
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()
