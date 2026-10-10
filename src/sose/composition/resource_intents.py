"""Opt-in durable admission of PC6 domain intents against finite PG resources.

An ACKed boundary intent is never discarded for lack of capacity. Admission
uses the authoritative temporal ledger, while a durable next-at record makes
contention observable and prevents a busy-loop on subsequent recovery ticks.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Mapping

from psycopg import sql

from sose.core.resource_identity import ResourcePoolContract
from sose.composition.effects import CERTIFIED_INTENTS
from sose.core.resource_reservations import (
    ResourceCapacityError, ResourceConflictError, TemporalReservation, _aware,
    authoritative_effect_reservation_id,
)


@dataclass(frozen=True, slots=True)
class DeferredResourceIntent:
    effect_id: str
    organization_id: str
    resource_key: str
    ready_at: datetime
    attempts: int


class IntentResourceCoordinator:
    """Scoped PostgreSQL admission/deferral for already-durable commands.

    Uses the SAME store/connection as the runner. A resource reservation is
    durable before domain execution. On worker death the same effect_id
    recovers the reservation; completed effects release the modeled interval.
    """

    def __init__(
        self,
        persistence,
        policies: Mapping[str, ResourcePoolContract],
        *,
        slot_duration: timedelta,
        retry_delay: timedelta,
        owner_epoch: int | None = None,
        fencing_persistence=None,
        effect_scope: str | None = None,
    ) -> None:
        if slot_duration <= timedelta(0) or retry_delay <= timedelta(0):
            raise ValueError("resource slot and retry duration must be positive")
        if not policies:
            raise ValueError("resource policy cannot be empty")
        if not hasattr(persistence, "temporal_resources") or not hasattr(persistence, "_connection"):
            raise TypeError("authoritative intent coordination requires PostgreSQL")
        self._store = persistence
        # A federated organization's writer epoch belongs to its domain store,
        # not to the separately owned shared physical resource ledger.
        self._fencing_persistence = fencing_persistence or persistence
        self._owner_epoch = owner_epoch
        self._effect_scope = effect_scope
        self._db = persistence._connection
        self._policies = dict(policies)
        if any(not k or not isinstance(v, ResourcePoolContract) for k,v in self._policies.items()):
            raise ValueError("invalid named domain resource policy")
        if not set(self._policies).issubset(CERTIFIED_INTENTS):
            raise ValueError("resource-mapped intents require immutable business certification")
        self._slot = slot_duration
        self._retry = retry_delay
        self._waiting = sql.Identifier(f"{persistence.namespace}_resource_intent_wait")
        self._links = sql.Identifier(f"{persistence.namespace}_resource_intent_link")
        self._positions = sql.Identifier(f"{persistence.namespace}_resource_org_position")
        with self._db.transaction():
            # Serialize only one-time DDL, not organizational resource actions.
            self._db.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (persistence.namespace,),
            )
            self._db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    effect_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    ready_at TIMESTAMPTZ NOT NULL,
                    attempts INTEGER NOT NULL CHECK (attempts > 0)
                )
            """).format(self._waiting))
            self._db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    organization_id TEXT PRIMARY KEY,
                    logical_time TIMESTAMPTZ NOT NULL
                )
            """).format(self._positions))
            self._db.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    effect_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    causation_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    local_effect_id TEXT,
                    admitted_trigger_id TEXT
                )
            """).format(self._links))
            # Existing authoritative single-store links must remain readable.
            self._db.execute(sql.SQL(
                "ALTER TABLE {} ADD COLUMN IF NOT EXISTS local_effect_id TEXT"
            ).format(self._links))
            self._db.execute(sql.SQL(
                "ALTER TABLE {} ADD COLUMN IF NOT EXISTS admitted_trigger_id TEXT"
            ).format(self._links))
        self._ledger = persistence.temporal_resources()
        for pool in self._policies.values():
            self._ledger.register_pool(pool)

    def _resource_id(self, effect_id: str) -> str:
        return authoritative_effect_reservation_id(effect_id, scope=self._effect_scope)

    def waiting(self, effect_id: str) -> DeferredResourceIntent | None:
        row = self._db.execute(sql.SQL("""
            SELECT effect_id, organization_id, resource_key, ready_at, attempts
              FROM {} WHERE effect_id = %s
        """).format(self._waiting), (self._resource_id(effect_id),)).fetchone()
        return DeferredResourceIntent(effect_id, *row[1:]) if row is not None else None

    def logical_time(self, organization_id: str) -> datetime | None:
        row = self._db.execute(sql.SQL("""
            SELECT logical_time FROM {} WHERE organization_id = %s
        """).format(self._positions), (organization_id,)).fetchone()
        return row[0] if row is not None else None

    def _advance_clock(self, organization_id: str, at: datetime) -> None:
        with self._db.transaction():
            self._db.execute(sql.SQL("""
                INSERT INTO {} (organization_id, logical_time) VALUES (%s,%s)
                ON CONFLICT (organization_id) DO UPDATE
                    SET logical_time = GREATEST({}.logical_time, EXCLUDED.logical_time)
            """).format(self._positions, self._positions), (organization_id, at))

    def _bind(self, effect_id: str, organization_id: str,
              resource_key: str, causation_id: str | None,
              correlation_id: str | None) -> None:
        if not correlation_id:
            raise ValueError("mapped intents require explicit causal correlation")
        if not causation_id:
            raise ValueError("mapped resource effects require immutable boundary causation")
        with self._db.transaction():
            previous = self._db.execute(sql.SQL("""
                SELECT organization_id, resource_key, causation_id, correlation_id,
                       COALESCE(local_effect_id, effect_id)
                  FROM {} WHERE effect_id = %s FOR UPDATE
            """).format(self._links), (self._resource_id(effect_id),)).fetchone()
            expected = (organization_id, resource_key, causation_id, correlation_id, effect_id)
            if previous is not None:
                if previous != expected:
                    raise ResourceConflictError("immutable resource intent ownership/causation changed")
                return
            self._db.execute(sql.SQL("""
                INSERT INTO {} (effect_id, organization_id, resource_key, causation_id,
                                correlation_id, local_effect_id)
                VALUES (%s,%s,%s,%s,%s,%s)
            """).format(self._links), (self._resource_id(effect_id), *expected))

    def _defer(self, effect_id: str, organization_id: str,
               resource_key: str, now: datetime) -> None:
        ready_at = now + self._retry
        with self._db.transaction():
            row = self._db.execute(sql.SQL("""
                SELECT organization_id, resource_key FROM {} WHERE effect_id=%s FOR UPDATE
            """).format(self._waiting), (self._resource_id(effect_id),)).fetchone()
            if row is not None and row != (organization_id, resource_key):
                raise ResourceConflictError("effect changed organizational resource ownership")
            self._db.execute(sql.SQL("""
                INSERT INTO {} (effect_id, organization_id, resource_key, ready_at, attempts)
                VALUES (%s,%s,%s,%s,1)
                ON CONFLICT (effect_id) DO UPDATE
                SET ready_at=EXCLUDED.ready_at, attempts={}.attempts+1
            """).format(self._waiting, self._waiting),
            (self._resource_id(effect_id), organization_id, resource_key, ready_at))

    def _writer_transaction(self):
        # The runner's epoch is held throughout any admission or completion.
        # The shared PG writer lock permits independent pool transactions,
        # but fences workers whose epoch was superseded.
        if self._owner_epoch is None:
            return nullcontext()
        return self._fencing_persistence.transaction(owner_epoch=self._owner_epoch)

    def admit(self, *, effect_id: str, intent_name: str,
              organization_id: str, due_at: datetime, now: datetime,
              causation_id: str | None = None,
              correlation_id: str | None = None,
              trigger_id: str | None = None) -> bool:
        """True: reservation durable and effect can run. False: wait is durable."""
        with self._writer_transaction():
            return self._admit(
                effect_id=effect_id, intent_name=intent_name,
                organization_id=organization_id, due_at=due_at, now=now,
                causation_id=causation_id, correlation_id=correlation_id,
                trigger_id=trigger_id,
            )

    def _admit(self, *, effect_id: str, intent_name: str,
               organization_id: str, due_at: datetime, now: datetime,
               causation_id: str | None, correlation_id: str | None,
               trigger_id: str | None = None) -> bool:
        if not effect_id or not organization_id:
            raise ValueError("effect_id and organization_id must be nonempty")
        if trigger_id is not None and not trigger_id:
            raise ValueError("admitted trigger identity must be nonempty")
        _aware(now, "now")
        _aware(due_at, "due_at")
        pool = self._policies.get(intent_name)
        if pool is None:
            return True  # Unmapped intents retain their historical semantics.
        key = pool.address().lock_key()
        # This immutable link is the ownership boundary for reconciliation,
        # including a worker death before or after admission COMMIT.
        self._bind(effect_id, organization_id, key, causation_id, correlation_id)
        if trigger_id is not None:
            row = self._db.execute(sql.SQL("""
                SELECT admitted_trigger_id FROM {} WHERE effect_id = %s
            """).format(self._links), (self._resource_id(effect_id),)).fetchone()
            if row is None:
                raise ResourceConflictError("immutable resource intent link disappeared")
            if row[0] is not None and row[0] != trigger_id:
                raise ResourceConflictError(
                    "physical effect was admitted by a different recurring trigger"
                )
        current_wait = self.waiting(effect_id)
        if current_wait is not None:
            if current_wait.organization_id != organization_id:
                raise ResourceConflictError("effect's organizational ownership changed")
            if current_wait.resource_key != key:
                raise ResourceConflictError("effect's durable waiting resource changed")
            if now < current_wait.ready_at:
                return False
        resource_id = self._resource_id(effect_id)
        existing = self._ledger.get(resource_id)
        if existing is not None:
            if existing.owner_id != organization_id:
                raise ResourceConflictError("effect's organizational ownership changed")
            if existing.address != pool.address():
                raise ResourceConflictError("effect's resource pool identity changed")
            if existing.causation_id != causation_id:
                raise ResourceConflictError("resource intent's causal predecessor changed")
            if existing.status not in ("reserved", "released"):
                raise ResourceConflictError("reserved intent was failed or preempted")
        else:
            start = max(now, due_at, self.logical_time(organization_id) or due_at)
            request = TemporalReservation(
                reservation_id=resource_id, address=pool.address(),
                owner_id=organization_id, start_at=start,
                end_at=start + self._slot,
                causation_id=causation_id,
            )
            try:
                existing = self._ledger.reserve(request)
            except ResourceCapacityError:
                self._defer(effect_id, organization_id, key, now)
                return False
        with self._db.transaction():
            self._db.execute(
                sql.SQL("DELETE FROM {} WHERE effect_id=%s").format(self._waiting),
                (resource_id,),
            )
        self._advance_clock(organization_id, existing.start_at)
        if trigger_id is not None:
            # Durable enrollment precedes real domain execution. A successful
            # effect cannot vanish from an interrupted recurring slot merely
            # because the physical booking has already been released.
            with self._db.transaction():
                row = self._db.execute(sql.SQL("""
                    UPDATE {} SET admitted_trigger_id = %s
                    WHERE effect_id = %s
                      AND (admitted_trigger_id IS NULL OR admitted_trigger_id = %s)
                    RETURNING effect_id
                """).format(self._links),
                    (trigger_id, resource_id, trigger_id)).fetchone()
                if row is None:
                    raise ResourceConflictError(
                        "physical effect was admitted by a different recurring trigger"
                    )
        return True

    def booking_for(self, effect_id: str, intent_name: str) -> TemporalReservation | None:
        """Re-read the live authoritative grant, never trust an in-memory claim."""
        pool = self._policies.get(intent_name)
        if pool is None:
            return None
        booking = self._ledger.get(self._resource_id(effect_id))
        if (
            booking is None or booking.status != "reserved"
            or booking.address != pool.address()
        ):
            raise ResourceConflictError("mapped intent has no live authoritative reservation")
        return booking

    def complete(self, effect_id: str) -> None:
        """Idempotent after a worker died following completion/commit."""
        with self._writer_transaction():
            self._complete(effect_id)

    def _complete(self, effect_id: str) -> None:
        resource_id = self._resource_id(effect_id)
        reservation = self._ledger.get(resource_id)
        if reservation is None:
            return  # Intent had no mapped resource.
        if reservation.status == "reserved":
            self._ledger.release(resource_id, at=reservation.end_at)
        elif reservation.status != "released":
            raise ResourceConflictError("effect's resource was terminated by another cause")
        self._advance_clock(reservation.owner_id, reservation.end_at)

    def reconcile_certified(
        self, store, organizations: Mapping[str, str], *,
        max_completed: int | None = None,
        trigger_id: str | None = None,
    ) -> int:
        """Reconcile only resource reservations explicitly linked to this runner.

        Shared pools may contain unrelated jobs' bookings; an address match
        alone never authorizes a runner to modify their business resources.
        """
        if max_completed is not None and max_completed < 1:
            raise ValueError("max_completed must be >= 1")
        completed = 0
        pool_keys = {pool.address().lock_key() for pool in self._policies.values()}
        links = self._db.execute(sql.SQL("""
            SELECT effect_id, COALESCE(local_effect_id, effect_id),
                   organization_id, resource_key, causation_id, correlation_id,
                   admitted_trigger_id
              FROM {} ORDER BY effect_id
        """).format(self._links)).fetchall()
        for (resource_id, effect_id, organization_id, resource_key,
             causation_id, correlation_id, admitted_trigger_id) in links:
            if max_completed is not None and completed >= max_completed:
                break
            if resource_id != self._resource_id(effect_id):
                continue  # Another organization's operational namespace.
            if resource_key not in pool_keys or correlation_id not in organizations:
                continue
            if organizations[correlation_id] != organization_id:
                raise ResourceConflictError("resource reservation certificate organization mismatch")
            reservation = self._ledger.get(resource_id)
            if reservation is None or reservation.status not in ("reserved", "released"):
                continue
            if reservation.status == "released" and (
                trigger_id is None or admitted_trigger_id != trigger_id
            ):
                # Older, already released work is not charged to a new slot.
                continue
            with store.transaction() as uow:
                command = uow.get_command(effect_id)
                certificate = uow.get_business_effect(effect_id)
            if (reservation.owner_id != organization_id
                or reservation.address.lock_key() != resource_key
                or reservation.causation_id != causation_id):
                raise ResourceConflictError("resource link contradicts immutable reservation")
            if command is not None:
                continue
            if certificate is None:
                raise ResourceConflictError(
                    "linked resource effect lacks both durable Command and business certificate"
                )
            if certificate.correlation_id != correlation_id:
                raise ResourceConflictError("resource reservation certificate correlation mismatch")
            if certificate.boundary_message_id != causation_id:
                raise ResourceConflictError("resource reservation certificate causation mismatch")
            if organizations.get(certificate.correlation_id) != organization_id:
                raise ResourceConflictError("resource reservation certificate organization mismatch")
            if reservation.status == "reserved":
                self.complete(effect_id)
            # A recovered, already released certified effect still consumes
            # the unfinished trigger's durable action budget if enrollment
            # proves it belongs to that SAME occurrence.
            completed += 1
        return completed
