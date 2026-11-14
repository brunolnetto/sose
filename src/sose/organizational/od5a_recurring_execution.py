"""Commit exactly one claimed recurring OD-5a source step via ephemeral SimPy.

Split out of od5a_recurring, which re-exports every name. The sequence of
fenced transactions, SimPy advancement and checks is the original one:
read due -> advance SimPy outside the DB -> preflight snapshot -> native
prepare outside the DB -> fenced commit inside the SimPy callback, or a
durable shared-capacity wait after a rejected shared booking.
"""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Literal

from sose.backends.simpy import SimPyBackend
from sose.core.identity import deterministic_id
from sose.core.resource_reservations import ResourceCapacityError
from sose.core.runtime import ResourceReservation
from sose.domain.entity import Entity
from sose.jobs.model import SimulationJobState
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_durable import _KIND, _ORIGIN, _advance_od5a_in_uow, _time, _transition
from .od5a_native_arbiter import _PhysicalStepPreview, _RecurringNativePhysicalArbiter
from .od5a_recurring_fence import (
    _SHARED_WAIT_KIND,
    OD5aRunnerClaim,
    StaleOD5aRunnerClaimError,
    _assert_claim,
    _job_transaction,
    _require_wall_clock,
    _trusted_now,
)
from .od5a_shared_authority import (
    SharedPhysicalGrantAuthority,
    SharedPhysicalPoolBinding,
    _assert_shared_binding,
)
from .od5a_shared_wait import (
    _require_shared_wait_marker,
    _source_wait_digest,
    _validate_shared_wait_record,
)


@dataclass(frozen=True, slots=True)
class OD5aRecurringStep:
    organization_id: str
    trigger_id: str
    action: Literal["advanced", "complete", "replayed", "waiting"]
    epoch: int
    logical_time: datetime
    logical_tick: int


def _next_action(actions: list[dict]) -> dict:
    return min(actions, key=lambda x: (x["at"], x["seq"]))


def execute_od5a_claimed_tick(
    store: Persistence, *, claim: OD5aRunnerClaim, now: datetime,
    _wall_clock: Callable[[], datetime] | None = None,
    shared_pool_binding: SharedPhysicalPoolBinding | None = None,
) -> OD5aRecurringStep:
    """Use a fresh ephemeral SimPy dispatch to commit exactly one source step.

    A kill after claim or inside callback leaves the uncommitted action
    retriable with a higher epoch; a kill after COMMIT leaves the checkpoint
    and the physical event inseparable.
    """
    _require_wall_clock(now)
    tick = _ClaimedTick(store, claim, _wall_clock, shared_pool_binding)
    tick.read_due()
    backend = SimPyBackend(origin=_ORIGIN)
    # A preflight hook or another worker may commit while SimPy advances
    # to the due boundary. Never hold a database write transaction during
    # this disposable, externally interceptable scheduling step.
    backend.run_until(_time(tick.due))
    tick.preflight(backend)
    # Native acquisitions and their potentially adversarial callbacks are
    # processed with NO SQLite BEGIN IMMEDIATE or PostgreSQL owner guard held.
    tick.native.prepare(
        releases=tuple(tick.preview.releases), grants=tuple(tick.preview.grants),
        at=tick.due,
    )
    tick.plan_wait()
    backend.schedule_at(
        _time(tick.due), tick.commit_source_step,
        key=("od5a-recurring", claim.organization_id, claim.trigger_id, claim.epoch),
    )
    try:
        backend.run_until(_time(tick.due))
    except ResourceCapacityError:
        if tick.authority is None or not tick.wait_requests or tick.wait_attributes is None:
            raise
        return tick.record_wait()
    if len(tick.dispatched) != 1:
        raise RuntimeError("native SimPy did not dispatch one authoritative OD-5a source action")
    return tick.dispatched[0]


class _ClaimedTick:
    """State shared by the phases of one claimed tick (formerly closure cells)."""

    def __init__(
        self, store: Persistence, claim: OD5aRunnerClaim,
        wall_clock: Callable[[], datetime] | None,
        binding: SharedPhysicalPoolBinding | None,
    ) -> None:
        self.store = store
        self.claim = claim
        self.job_id = claim.organization_id
        self.wall_clock = wall_clock
        self.binding = binding
        self.authority = (
            SharedPhysicalGrantAuthority(store, binding, claim.organization_id)
            if binding is not None else None
        )
        self.dispatched: list[OD5aRecurringStep] = []

    def _fenced(
        self, uow: UnitOfWork, at: datetime | None = None,
    ) -> tuple[Entity, SimulationJobState, Entity]:
        if at is None:
            at = _trusted_now(self.store, self.wall_clock)
        source, job, lease = _assert_claim(uow, self.claim, at)
        _assert_shared_binding(source, self.binding)
        return source, job, lease

    def _owned(self, uow: UnitOfWork) -> tuple[ResourceReservation, ...]:
        return tuple(
            item for item in uow.resource_reservations()
            if item.resource_name in self.owned_names
        )

    def read_due(self) -> None:
        with _job_transaction(self.store, self.job_id) as uow:
            source, _, _ = self._fenced(uow)
            actions = source.attributes["actions"]
            self.due = _next_action(actions)["at"] if actions else source.attributes["clock"]
            # Capture the COMPLETE committed action, not just at/seq/kind.
            # Otherwise a changed arrival item or completion resource can cross
            # the read-side preflight -> ephemeral SimPy dispatch boundary.
            self.next_action = dict(_next_action(actions)) if actions else None

    def preflight(self, backend: SimPyBackend) -> None:
        # Plan the reducer's upcoming leases using only a disposable ledger.
        # No SimPy callbacks execute while reading the transactional snapshot.
        with _job_transaction(self.store, self.job_id) as uow:
            source, _, _ = self._fenced(uow)
            actions = source.attributes["actions"]
            actual = _next_action(actions) if actions else None
            if actual != self.next_action:
                raise StaleOD5aRunnerClaimError(
                    "OD-5a scheduled step changed before SimPy dispatch"
                )
            self.preflight_state = deepcopy(source.attributes)
            # Compare only this organization's physical addresses. An independent
            # owner can legitimately commit disjoint reservations between preflight
            # and this worker's fenced commit; no namespace-wide serialization.
            self.owned_names = {
                f"{self.job_id}/{slot}" for slot in self.preflight_state["resources"]
            }
            self.preflight_reservations = self._owned(uow)
            self.preview = _PhysicalStepPreview(self.preflight_reservations)
            self.planned_state = deepcopy(self.preflight_state)
            _transition(self.preview, self.planned_state, self.job_id)
            self.native = _RecurringNativePhysicalArbiter(
                backend, definitions=uow.resource_definitions(),
                reservations=self.preflight_reservations,
                resources=self.preflight_state["resources"],
                job_id=self.job_id, due=self.due,
            )

    def plan_wait(self) -> None:
        # A rejected shared PostgreSQL booking is not a failed business event.
        # Record exact source/physical request identity without advancing the
        # source clock or pretending that an ephemeral SimPy permit was granted.
        authority = self.authority
        self.wait_id = deterministic_id(
            "od5a-shared-capacity-wait", self.job_id, self.claim.trigger_id,
        )
        self.wait_requests = sorted((
            {
                "resource_name": grant.resource_name,
                "reservation_id": grant.reservation_id,
                "request_id": grant.request_id,
                "instance_id": slot[1],
            }
            for grant in self.preview.grants
            if authority is not None
            and (slot := authority._slot(grant.resource_name)) is not None
        ), key=lambda request: request["reservation_id"])
        self.wait_attributes = (
            {
                "trigger_id": self.claim.trigger_id,
                "binding_fingerprint": self.binding.fingerprint(),
                "source_action": self.next_action,
                "due_at": _time(self.due).isoformat(),
                "requests": self.wait_requests,
                "wait_schema_version": 2,
                "source_digest": _source_wait_digest(self.preflight_state),
            }
            if authority is not None else None
        )

    def _validate_wait(self, wait: Entity, *, epoch: int) -> None:
        _validate_shared_wait_record(wait, expected=self.wait_attributes, epoch=epoch)

    def commit_source_step(self) -> None:
        with _job_transaction(self.store, self.job_id) as uow:
            # Re-read the trusted clock AFTER the lock. Neither invocation
            # time nor the worker host's clock can extend/stale the fence.
            commit_at = _trusted_now(self.store, self.wall_clock)
            source, job, lease = self._fenced(uow, commit_at)
            self._require_unchanged_since_preflight(uow, source)
            # Reconstruct the durable wait/lease cross-reference BEFORE
            # attempting any real physical grant, even when an entity was
            # removed by an independent corrupted writer.
            wait = self._checked_wait(uow, lease) if self.authority else None
            # The per-pool guard and source UoW share one PostgreSQL
            # transaction. The guard is scoped to THIS physical pool only.
            # A collision must roll back physical booking AND source/job.
            with (self.authority.guarded_pool() if self.authority else nullcontext()):
                advanced, committed = self._advance_and_book(uow, wait, commit_at)
            self._release_checkpoint(uow, job, lease, committed, commit_at, advanced)

    def _require_unchanged_since_preflight(self, uow: UnitOfWork, source: Entity) -> None:
        actions = source.attributes["actions"]
        actual = _next_action(actions) if actions else None
        if actual != self.next_action or source.attributes != self.preflight_state:
            raise StaleOD5aRunnerClaimError(
                "OD-5a source changed across native physical acquisition"
            )
        if self._owned(uow) != self.preflight_reservations:
            raise StaleOD5aRunnerClaimError(
                "OD-5a own physical ledger changed before native commit"
            )

    def _checked_wait(self, uow: UnitOfWork, lease: Entity) -> Entity | None:
        wait = uow.get_entity(_SHARED_WAIT_KIND, self.wait_id)
        _require_shared_wait_marker(
            lease, wait_id=self.wait_id, wait_exists=wait is not None,
        )
        if wait is not None:
            self._validate_wait(wait, epoch=self.claim.epoch)
        return wait

    def _advance_and_book(
        self, uow: UnitOfWork, wait: Entity | None, commit_at: datetime,
    ) -> tuple[bool, Entity]:
        advanced = _advance_od5a_in_uow(uow, job_id=self.job_id, native=self.native)
        self.native.verify_committed_step()
        committed = uow.get_entity(_KIND, self.job_id)
        assert committed is not None
        if committed.attributes != self.planned_state:
            raise RuntimeError(
                "native source truth differs from deterministic planned grant"
            )
        if self.authority is not None:
            self.authority.apply(
                before=self.preflight_reservations,
                releases=tuple(self.preview.releases),
                grants=tuple(self.preview.grants),
                after_state=committed.attributes,
                at=self.due, uow=uow,
            )
            # The same physical/source SQL COMMIT resolves any
            # previously registered wait; a worker dying after COMMIT
            # must never leave an apparently pending request.
            if wait is not None:
                wait.state = "satisfied"
                wait.attributes = {
                    **wait.attributes, "last_claim_epoch": self.claim.epoch,
                }
                wait.touch(commit_at)
                uow.save_entity(wait)
        return advanced, committed

    def _release_checkpoint(
        self, uow: UnitOfWork, job: SimulationJobState, lease: Entity,
        committed: Entity, commit_at: datetime, advanced: bool,
    ) -> None:
        job = replace(
            job, status="ready", phase="idle", active_trigger_id=None,
            last_completed_trigger_id=self.claim.trigger_id,
            logical_time=_time(committed.attributes["clock"]),
            next_tick=committed.attributes["step"],
            run_count=job.run_count + 1,
            last_error=None,
        )
        uow.save_job_state(job)
        lease.state = "released"
        lease.attributes = {
            **{k: v for k, v in lease.attributes.items()
               if k != "physical_wait_id"},
            "owner_id": None,
        }
        lease.touch(commit_at)
        uow.save_entity(lease)
        self.dispatched.append(OD5aRecurringStep(
            self.job_id, self.claim.trigger_id,
            "advanced" if advanced else "complete",
            self.claim.epoch, job.logical_time, job.next_tick,
        ))

    def record_wait(self) -> OD5aRecurringStep:
        # The source+physical transaction has rolled back. Only the exact
        # still-owned trigger can durably acknowledge backpressure in a NEW
        # fenced transaction. Another org/pool never waits on this job lock.
        claim = self.claim
        with _job_transaction(self.store, self.job_id) as uow:
            source, job, lease = self._fenced(uow)
            if (
                source.attributes != self.preflight_state
                or self._owned(uow) != self.preflight_reservations
            ):
                raise StaleOD5aRunnerClaimError(
                    "OD-5a source/physical state changed before durable wait"
                )
            wait = self._checked_wait(uow, lease)
            # Fencing must use the timestamp of THIS durable write, not the
            # previous snapshot's clock; a claim can expire between the two.
            stamp = _trusted_now(self.store, self.wall_clock)
            _assert_claim(uow, claim, stamp)
            if wait is None:
                self._open_wait(uow, lease, stamp)
            elif wait.attributes["last_claim_epoch"] != claim.epoch:
                wait.attributes = {
                    **wait.attributes, "last_claim_epoch": claim.epoch,
                }
                wait.touch(stamp)
                uow.save_entity(wait)
            return OD5aRecurringStep(
                claim.organization_id, claim.trigger_id, "waiting",
                claim.epoch, job.logical_time, job.next_tick,
            )

    def _open_wait(self, uow: UnitOfWork, lease: Entity, stamp: datetime) -> None:
        wait = Entity(
            id=self.wait_id, entity_type=_SHARED_WAIT_KIND, state="waiting",
            attributes={
                **deepcopy(self.wait_attributes),
                "origin_epoch": self.claim.epoch,
                "last_claim_epoch": self.claim.epoch,
            },
        )
        wait.touch(stamp)
        uow.save_entity(wait)
        lease.attributes = {
            **lease.attributes, "physical_wait_id": self.wait_id,
        }
        lease.touch(stamp)
        uow.save_entity(lease)
