"""PostgreSQL-authoritative temporal resource ledger, additive to EnginePersistence.

All writes for one pool use a pool-scoped PostgreSQL transaction advisory lock.
Independent organizational pools do not take a global runtime mutex. Durable
reservation records and immutable causal events commit in the *same* transaction.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from contextlib import contextmanager
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

from .postgres_resource_ledger_audit import (
    check_booking_events,
    check_event_causality,
    check_outage_events,
    check_pool_history,
)
from .postgres_resource_ledger_fit import (
    advice_candidates,
    check_advice_pool,
    check_advice_request,
    check_reservation_pool,
    fits,
    outage_interrupts,
    overlap,
    pool_lock_key,
    select_victims,
)
from .postgres_resource_ledger_reads import LedgerReadsMixin
from .postgres_resource_ledger_schema import bootstrap_schema


class PostgresTemporalResourceLedger(LedgerReadsMixin):
    """Transactional allocation and recovery, using an existing PG connection.

    This is an additive authoritative subsystem. The generic SOSE snapshot
    serializer and logical SimulationPosition are not modified. A caller must
    not assume the resource ledger and unrelated Engine UoWs commit atomically.
    """

    def __init__(self, persistence, *, bootstrap: bool = True) -> None:
        self._store = persistence
        self._db = persistence._connection
        ns = persistence.namespace
        self._pools = sql.Identifier(f"{ns}_resource_pool")
        self._reservations = sql.Identifier(f"{ns}_resource_booking")
        self._outages = sql.Identifier(f"{ns}_resource_outage")
        self._events = sql.Identifier(f"{ns}_resource_event")
        # Read-side availability advice must not execute DDL or take a
        # namespace-wide transaction advisory lock. Existing tables are
        # established by explicit authoritative registration/write setup.
        if bootstrap:
            self._bootstrap()

    def _bootstrap(self) -> None:
        bootstrap_schema(
            self._db, self._store, pools=self._pools, reservations=self._reservations,
            outages=self._outages, events=self._events,
        )

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

    @contextmanager
    def guard_pool(self, pool: ResourcePoolContract):
        """Pin one immutable physical pool throughout its caller's SQL action.

        A nested transaction on the SAME PostgreSQL connection is a savepoint.
        The pool-scoped advisory lock survives until the enclosing source UoW
        commits or rolls back. Independent pools/organizations proceed without
        a global writer lock or globally synchronized simulation clock.
        """
        if not isinstance(pool, ResourcePoolContract):
            raise TypeError("authoritative guard requires ResourcePoolContract")
        key = self._key(pool)
        with self._db.transaction():
            self._lock(key)
            if self._pool(key) != pool:
                raise ResourceConflictError("shared physical pool contract changed")
            yield self

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

    @staticmethod
    def _overlap(a_start, a_end, b_start, b_end) -> bool:
        return overlap(a_start, a_end, b_start, b_end)

    @classmethod
    def _fits(
        cls, pool: ResourcePoolContract, requested: TemporalReservation,
        others: Iterable[TemporalReservation], outages: Iterable[TemporalOutage],
    ) -> bool:
        return fits(pool, requested, others, outages, overlaps=cls._overlap)

    def _event(self, event_id: str, key: str, subject: str, action: str,
               at: datetime, cause: str | None = None) -> None:
        self._db.execute(sql.SQL("""
            INSERT INTO {}(event_id, pool_key, subject_id, action,
                           occurred_at, causation_event_id)
            VALUES (%s,%s,%s,%s,%s,%s)
        """).format(self._events), (event_id, key, subject, action, at, cause))

    def suggest_next_start(
        self, request: TemporalReservation, *, not_before: datetime,
        latest_start: datetime,
    ) -> datetime | None:
        """Read-only, prospective interval feasibility *advice*, NOT a grant.

        Evaluate one exact immutable physical instance or fungible pool,
        preserving request duration, units and owner. Capacity can improve
        only when an existing booking or outage ends; enumerate those finite
        durable end boundaries rather than sampling a worker-local clock or
        rewinding the rejected request into the past.

        The result is intentionally NOT a reservation or a promise. Every
        consumer must separately admit a rescheduled source action, then
        call reserve in its own fenced authoritative UoW. Another owner may
        legitimately book the suggested interval immediately after this
        read; that transaction MUST still fail closed on physical overlap.
        """
        check_advice_request(request, not_before, latest_start)
        key = pool_lock_key(request.address)
        # Advisory hint only: intentionally DO NOT take a pool advisory lock.
        # In an outer SQL transaction that lock could escape the method and
        # block unrelated writers. The later authoritative reserve owns the
        # real lock and revalidates all capacity under concurrent writes.
        try:
            horizon_end = latest_start + (request.end_at - request.start_at)
        except OverflowError as exc:
            raise ValueError("prospective horizon overflows datetime") from exc
        with self._db.transaction():
            pool = self._pool(key)
            check_advice_pool(pool, request)
            if self._get_booking(request.reservation_id) is not None:
                raise ResourceConflictError(
                    "already-booked reservation identity cannot be rescheduled by advice"
                )
            bookings = self._window_bookings(
                key, pool, start=not_before, end=horizon_end,
            )
            outages = self._window_outages(
                key, pool, start=not_before, end=horizon_end,
            )
            duration = request.end_at - request.start_at
            candidates = advice_candidates(
                pool, request, bookings, outages,
                not_before=not_before, latest_start=latest_start,
            )
            for start in sorted(candidates):
                try:
                    proposal = replace(
                        request, start_at=start, end_at=start + duration,
                    )
                except (OverflowError, ValueError):
                    continue
                if self._fits(pool, proposal, bookings, outages):
                    return start
            return None

    def reserve(self, request: TemporalReservation, *, preempt: bool = False) -> TemporalReservation:
        if not isinstance(request, TemporalReservation) or request.status != "reserved":
            raise ValueError("reserve requires a new active TemporalReservation")
        key = pool_lock_key(request.address)
        with self._db.transaction():
            self._lock(key)
            pool = self._pool(key)
            check_reservation_pool(pool, request)
            existing = self._get_booking(request.reservation_id)
            if existing is not None:
                if replace(existing, status="reserved", stopped_at=None) != request:
                    raise ResourceConflictError("reservation identity reused with different contents")
                return existing  # Retry after committed write or worker death.
            bookings = list(self._bookings(key))
            outages = self._outages_for(key)
            victims = []
            if not self._fits(pool, request, bookings, outages):
                if not preempt:
                    raise ResourceCapacityError("finite resource unavailable for interval")
                victims = select_victims(
                    pool, request, bookings, outages,
                    fits_=self._fits, overlaps=self._overlap,
                )
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

    @contextmanager
    def authorize_use(self, expected: TemporalReservation, *, at: datetime):
        """Pin a live booking against release/failure while its domain transition commits.

        Uses the same pool-scoped transaction lock as reserve/release/fail. When
        the caller dispatches through the *same* persistence connection,
        nested Engine UoWs are savepoints of this transaction: the validated
        resource and the business transition are committed or rolled back
        together, without serializing unrelated pools.
        """
        if not isinstance(expected, TemporalReservation):
            raise TypeError("authoritative use requires TemporalReservation")
        _aware(at, "at")
        key = replace(expected.address, instance_id=None).lock_key()
        with self._db.transaction():
            self._lock(key)
            actual = self._get_booking(expected.reservation_id)
            if (
                actual != expected or actual.status != "reserved"
                or not actual.start_at <= at < actual.end_at
            ):
                raise ResourceConflictError(
                    "authoritative pickup lacks a live matching temporal reservation"
                )
            yield actual

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
                if not outage_interrupts(outage, item, self._overlap):
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

    def audit(self) -> str:
        """Reject overcommit/orphan causal events; return semantic SHA-256.

        Audit should run on a quiesced database or a transactionally consistent
        snapshot; interleaving read-only statements across writers is not proof.
        """
        snapshot = self.snapshot()
        ids = {event[0] for event in snapshot["events"]}
        check_event_causality(snapshot["events"], ids)
        check_booking_events(self.reservations(), snapshot["events"], ids)
        check_outage_events(self.outages(), ids)
        for key, serialized in snapshot["pools"]:
            data = dict(serialized)
            data["instance_ids"] = tuple(data["instance_ids"])
            pool = ResourcePoolContract(**data)
            bookings = [b for b in self.reservations()
                        if replace(b.address, instance_id=None).lock_key() == key]
            outages = [o for o in self.outages()
                       if replace(o.address, instance_id=None).lock_key() == key]
            check_pool_history(pool, bookings, outages, overlaps=self._overlap)
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()
