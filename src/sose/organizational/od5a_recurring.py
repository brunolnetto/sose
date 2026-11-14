"""Per-organization recurring OD-5a source step driven by ephemeral native SimPy.

First bounded #477 native-grant slice: the authoritative reducer still
selects a deterministic free physical instance and owns causal transitions,
but a recurring source tick may NOT certify a new ResourceReservation unless
the ephemeral SimPy PriorityResource grants the exact same request/instance.
Preexisting occupancy is reconstructed from the durable ledger each tick.
This is NOT yet independent native pool scheduling, durable wait/fairness,
preemption, network/uncertain-COMMIT qualification or full OD-5a admission.

A scoped durable lease and SimulationJobState checkpoint fence a worker. The
physical event, resource ledger and job checkpoint commit in ONE transaction.
No SimPy Environment, Process, Event or callback is persisted.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from contextlib import nullcontext
from json import dumps
from typing import Callable, Literal

from sose.backends.simpy import SimPyBackend
from sose.core.identity import deterministic_id
from sose.core.resource_reservations import (
    ResourceCapacityError,
)
from sose.domain.entity import Entity
from sose.jobs.model import SimulationJobState
from sose.jobs.registration import job_registration_guard
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_admission import OD5aCompiledArm
from .od5a_durable import (
    _KIND, _ORIGIN, _time, _advance_od5a_in_uow, _transition,
    initialize_od5a_durable,
)
from .od5a_mechanisms import OD5aWorkItem
from .od5a_native_arbiter import _PhysicalStepPreview, _RecurringNativePhysicalArbiter
from .od5a_shared_authority import (
    SharedPhysicalGrantAuthority,
    SharedPhysicalPoolBinding,
    _assert_shared_binding,
)
from .od5a_shared_wait import (
    SharedPhysicalAlignedWaitAdvice,
    SharedPhysicalWaitSlotAdvice,
    _aligned_physical_start,  # noqa: F401 - re-exported
    _require_shared_wait_marker,
    _source_wait_digest,
    _validate_shared_wait_record,
    suggest_od5a_aligned_wait_start,
    suggest_od5a_shared_wait_slots,
)
from .od5a_recurring_fence import (
    _SHARED_WAIT_KIND,
    _job_transaction,
    _DOMAIN,
    _LEASE_KIND,
    OD5aRunnerClaim,
    StaleOD5aRunnerClaimError,
    _assert_active_claimed_lease,  # noqa: F401 - re-exported
    _assert_claim,
    _assert_completed_released_lease,
    _config,
    _lease_expiry,  # noqa: F401 - re-exported
    _next_trigger_id,  # noqa: F401 - re-exported
    _read_job,
    _require_wall_clock,
    _trigger_claimable,
    _trusted_now,
    _validated_prior_lease,
)


@dataclass(frozen=True, slots=True)
class OD5aRecurringStep:
    organization_id: str
    trigger_id: str
    action: Literal["advanced", "complete", "replayed", "waiting"]
    epoch: int
    logical_time: datetime
    logical_tick: int


def initialize_od5a_recurring_job(
    store: Persistence, *, job_id: str, arm: OD5aCompiledArm,
    items: tuple[OD5aWorkItem, ...],
    shared_pool_binding: SharedPhysicalPoolBinding | None = None,
) -> SimulationJobState:
    """Atomically commit an immutable source and its owning job checkpoint.

    A foreign SimulationJobState must not leave an orphaned pilot or resource
    definitions after failed registration. The binder runs inside the SAME
    authoritative source transaction, protected by the per-organization
    PostgreSQL guard when available.
    """
    registered: list[SimulationJobState] = []
    if shared_pool_binding is not None:
        if not isinstance(shared_pool_binding, SharedPhysicalPoolBinding):
            raise TypeError("shared_pool_binding must be SharedPhysicalPoolBinding")
        # The pool is a separately existing immutable physical asset. Each
        # organization binds to its identity, never invents a local copy.
        SharedPhysicalGrantAuthority(
            store, shared_pool_binding, job_id,
        ).ledger.register_pool(shared_pool_binding.pool)

    def bind(uow: UnitOfWork) -> None:
        source = uow.get_entity(_KIND, job_id)
        assert source is not None
        if shared_pool_binding is None:
            _assert_shared_binding(source, None)
        else:
            physical_slots = {
                name for name in source.attributes["resources"]
                if name.startswith("execution:")
            }
            bound_slots = {
                name for name, _ in shared_pool_binding.execution_instances
            }
            if physical_slots != bound_slots:
                raise ValueError("shared binding does not cover exact source execution slots")
            if source.attributes.get("shared_pool_binding") is None:
                if source.attributes["step"] != 0:
                    raise ValueError("shared authority cannot adopt historical source actions")
                source.attributes["shared_pool_binding"] = (
                    shared_pool_binding.fingerprint()
                )
                uow.save_entity(source)
            else:
                _assert_shared_binding(source, shared_pool_binding)
        registered.append(_register_od5a_job_in_uow(uow, job_id=job_id))

    # First claim the job identity across ALL domain creators; only then
    # enter the OD-5a physical/source fence. Fixed acquisition order prevents
    # the generic runner from racing a foreign job into this registration.
    with job_registration_guard(store, job_id):
        guard = getattr(store, "business_resource_guard", None)
        if callable(guard):
            with guard("od5a:recurring:" + job_id):
                initialize_od5a_durable(
                    store, job_id=job_id, arm=arm, items=items, _bind_uow=bind,
                )
        else:
            initialize_od5a_durable(
                store, job_id=job_id, arm=arm, items=items, _bind_uow=bind,
            )
    assert len(registered) == 1
    return registered[0]


def _register_od5a_job_in_uow(
    uow: UnitOfWork, *, job_id: str,
) -> SimulationJobState:
    # Shared transaction with physical source creation, no orphan on failure.
    source = uow.get_entity(_KIND, job_id)
    assert source is not None
    prior = uow.get_job_state(job_id)
    if prior is not None:
        if prior.domain_name != _DOMAIN:
            raise ValueError(
                "OD-5a organization is owned by another domain's SimulationJobState"
            )
        _read_job(uow, job_id)
        return prior
    if source.attributes["step"] != 0:
        raise ValueError("OD-5a recurring job cannot adopt untracked physical history")
    identity = _config(source)
    state = SimulationJobState(
        job_id=job_id,
        domain_name=_DOMAIN,
        config_json=dumps(identity, sort_keys=True, separators=(",", ":")),
        config_revision=1,
        status="ready",
        initialized=True,
        bootstrap_state=identity,
        logical_time=_time(source.attributes["clock"]),
        next_tick=source.attributes["step"],
    )
    uow.save_job_state(state)
    return state

def claim_od5a_recurring_tick(
    store: Persistence, *, job_id: str, owner_id: str,
    now: datetime, lease_duration: timedelta,
    trigger_id: str, recover: bool = False,
    _wall_clock: Callable[[], datetime] | None = None,
) -> OD5aRunnerClaim | None:
    """Claim the next exact source-step ID with a durable job-scoped epoch.

    A previously committed trigger is never admitted again; after a worker
    dies before source COMMIT, recovery must explicitly opt in and wait for
    the prior lease expiry.
    """
    _require_wall_clock(now, lease_duration)
    if not owner_id or not job_id or not trigger_id:
        raise ValueError("runner owner, organization and stable trigger ID must be nonempty")
    with _job_transaction(store, job_id) as uow:
        trusted_now = _trusted_now(store, _wall_clock)
        source, job = _read_job(uow, job_id)
        _assert_completed_released_lease(uow, job)
        if not _trigger_claimable(source, job, trigger_id):
            return None

        existing = uow.get_entity(_LEASE_KIND, job_id)
        epoch = 1
        pending_physical_wait_id = None
        if existing is not None:
            succession = _succeed_prior_lease(
                existing, job, trusted_now, recover=recover,
                claim=OD5aRunnerClaim(job_id, trigger_id, owner_id, 0, trusted_now),
            )
            if isinstance(succession, OD5aRunnerClaim):
                return succession
            epoch, pending_physical_wait_id = succession
        elif job.active_trigger_id is not None:
            raise StaleOD5aRunnerClaimError("unfinished source trigger lost authoritative lease")

        expires_at = trusted_now + lease_duration
        lease = existing or Entity(
            id=job_id, entity_type=_LEASE_KIND, state="released",
        )
        lease.state = "claimed"
        lease.attributes = _claimed_lease_attributes(
            owner_id, epoch, expires_at, trigger_id, pending_physical_wait_id,
        )
        lease.touch(trusted_now)
        uow.save_entity(lease)
        uow.save_job_state(replace(
            job, status="running", phase="advance", active_trigger_id=trigger_id,
            last_triggered_at=trusted_now, last_error=None,
        ))
        return OD5aRunnerClaim(job_id, trigger_id, owner_id, epoch, expires_at)


def _succeed_prior_lease(
    existing: Entity, job: SimulationJobState, trusted_now: datetime, *,
    recover: bool, claim: OD5aRunnerClaim,
) -> OD5aRunnerClaim | tuple[int, object]:
    """Re-enter the caller's own live lease, or (next epoch, pending wait ID).

    ``claim`` carries the requested identity; its epoch and expiry are ignored.
    """
    epoch, expiry, previous_owner, pending_physical_wait_id = (
        _validated_prior_lease(existing, job, claim.organization_id)
    )
    if trusted_now < existing.updated_at:
        raise ValueError("trusted time precedes durable OD-5a runner lease timestamp")
    if previous_owner is not None and trusted_now < expiry:
        # Only the live owner of this very trigger may re-enter its lease.
        if previous_owner == claim.owner_id and job.active_trigger_id == claim.trigger_id:
            return replace(claim, epoch=epoch, lease_expires_at=expiry)
        raise StaleOD5aRunnerClaimError("active OD-5a runner lease belongs to another worker")
    if job.active_trigger_id is not None and not recover:
        raise StaleOD5aRunnerClaimError("unfinished source trigger requires explicit recovery")
    return epoch + 1, pending_physical_wait_id


def _claimed_lease_attributes(
    owner_id: str, epoch: int, expires_at: datetime, trigger_id: str,
    pending_physical_wait_id: object,
) -> dict[str, object]:
    return {
        "owner_id": owner_id,
        "epoch": epoch,
        "expires_at": expires_at.isoformat(),
        "trigger_id": trigger_id,
        **({
            "physical_wait_id": pending_physical_wait_id,
        } if pending_physical_wait_id is not None else {}),
    }


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
    authority = (
        SharedPhysicalGrantAuthority(store, shared_pool_binding, claim.organization_id)
        if shared_pool_binding is not None else None
    )
    with _job_transaction(store, claim.organization_id) as uow:
        source, _, _ = _assert_claim(
            uow, claim, _trusted_now(store, _wall_clock),
        )
        _assert_shared_binding(source, shared_pool_binding)
        actions = source.attributes["actions"]
        due = (
            min(actions, key=lambda x: (x["at"], x["seq"]))["at"]
            if actions else source.attributes["clock"]
        )
        # Capture the COMPLETE committed action, not just at/seq/kind.
        # Otherwise a changed arrival item or completion resource can cross
        # the read-side preflight -> ephemeral SimPy dispatch boundary.
        next_action = (
            dict(min(actions, key=lambda x: (x["at"], x["seq"])))
            if actions else None
        )
    dispatched: list[OD5aRecurringStep] = []
    backend = SimPyBackend(origin=_ORIGIN)
    # A preflight hook or another worker may commit while SimPy advances
    # to the due boundary. Never hold a database write transaction during
    # this disposable, externally interceptable scheduling step.
    backend.run_until(_time(due))
    # Plan the reducer's upcoming leases using only a disposable ledger.
    # No SimPy callbacks execute while reading the transactional snapshot.
    with _job_transaction(store, claim.organization_id) as uow:
        source, _, _ = _assert_claim(
            uow, claim, _trusted_now(store, _wall_clock),
        )
        _assert_shared_binding(source, shared_pool_binding)
        actions = source.attributes["actions"]
        actual = min(actions, key=lambda x: (x["at"], x["seq"])) if actions else None
        if actual != next_action:
            raise StaleOD5aRunnerClaimError(
                "OD-5a scheduled step changed before SimPy dispatch"
            )
        preflight_state = deepcopy(source.attributes)
        # Compare only this organization's physical addresses. An independent
        # owner can legitimately commit disjoint reservations between preflight
        # and this worker's fenced commit; no namespace-wide serialization.
        owned_names = {
            f"{claim.organization_id}/{slot}"
            for slot in preflight_state["resources"]
        }
        preflight_reservations = tuple(
            item for item in uow.resource_reservations()
            if item.resource_name in owned_names
        )
        preview = _PhysicalStepPreview(preflight_reservations)
        planned_state = deepcopy(preflight_state)
        _transition(
            preview, planned_state,
            claim.organization_id,
        )
        native = _RecurringNativePhysicalArbiter(
            backend, definitions=uow.resource_definitions(),
            reservations=preflight_reservations,
            resources=preflight_state["resources"],
            job_id=claim.organization_id, due=due,
        )

    # Native acquisitions and their potentially adversarial callbacks are
    # processed with NO SQLite BEGIN IMMEDIATE or PostgreSQL owner guard held.
    native.prepare(
        releases=tuple(preview.releases), grants=tuple(preview.grants), at=due,
    )

    # A rejected shared PostgreSQL booking is not a failed business event.
    # Record exact source/physical request identity without advancing the
    # source clock or pretending that an ephemeral SimPy permit was granted.
    wait_id = deterministic_id(
        "od5a-shared-capacity-wait", claim.organization_id, claim.trigger_id,
    )
    wait_requests = sorted((
        {
            "resource_name": grant.resource_name,
            "reservation_id": grant.reservation_id,
            "request_id": grant.request_id,
            "instance_id": slot[1],
        }
        for grant in preview.grants
        if authority is not None
        and (slot := authority._slot(grant.resource_name)) is not None
    ), key=lambda request: request["reservation_id"])
    wait_attributes = (
        {
            "trigger_id": claim.trigger_id,
            "binding_fingerprint": shared_pool_binding.fingerprint(),
            "source_action": next_action,
            "due_at": _time(due).isoformat(),
            "requests": wait_requests,
            "wait_schema_version": 2,
            "source_digest": _source_wait_digest(preflight_state),
        }
        if authority is not None else None
    )

    def validate_wait(wait: Entity, *, epoch: int) -> None:
        _validate_shared_wait_record(
            wait, expected=wait_attributes, epoch=epoch,
        )

    def commit_source_step() -> None:
        with _job_transaction(store, claim.organization_id) as uow:
            # Re-read the trusted clock AFTER the lock. Neither invocation
            # time nor the worker host's clock can extend/stale the fence.
            commit_at = _trusted_now(store, _wall_clock)
            source, job, lease = _assert_claim(uow, claim, commit_at)
            _assert_shared_binding(source, shared_pool_binding)
            actions = source.attributes["actions"]
            actual = min(actions, key=lambda x: (x["at"], x["seq"])) if actions else None
            if actual != next_action or source.attributes != preflight_state:
                raise StaleOD5aRunnerClaimError(
                    "OD-5a source changed across native physical acquisition"
                )
            current_owned = tuple(
                item for item in uow.resource_reservations()
                if item.resource_name in owned_names
            )
            if current_owned != preflight_reservations:
                raise StaleOD5aRunnerClaimError(
                    "OD-5a own physical ledger changed before native commit"
                )
            # Reconstruct the durable wait/lease cross-reference BEFORE
            # attempting any real physical grant, even when an entity was
            # removed by an independent corrupted writer.
            wait = uow.get_entity(_SHARED_WAIT_KIND, wait_id) if authority else None
            if authority is not None:
                _require_shared_wait_marker(
                    lease, wait_id=wait_id, wait_exists=wait is not None,
                )
                if wait is not None:
                    validate_wait(wait, epoch=claim.epoch)
            # The per-pool guard and source UoW share one PostgreSQL
            # transaction. The guard is scoped to THIS physical pool only.
            # A collision must roll back physical booking AND source/job.
            with (authority.guarded_pool() if authority else nullcontext()):
                advanced = _advance_od5a_in_uow(
                    uow, job_id=claim.organization_id, native=native,
                )
                native.verify_committed_step()
                committed = uow.get_entity(_KIND, claim.organization_id)
                assert committed is not None
                if committed.attributes != planned_state:
                    raise RuntimeError(
                        "native source truth differs from deterministic planned grant"
                    )
                if authority is not None:
                    authority.apply(
                        before=preflight_reservations,
                        releases=tuple(preview.releases),
                        grants=tuple(preview.grants),
                        after_state=committed.attributes,
                        at=due, uow=uow,
                    )
                    # The same physical/source SQL COMMIT resolves any
                    # previously registered wait; a worker dying after COMMIT
                    # must never leave an apparently pending request.
                    if wait is not None:
                        wait.state = "satisfied"
                        wait.attributes = {
                            **wait.attributes, "last_claim_epoch": claim.epoch,
                        }
                        wait.touch(commit_at)
                        uow.save_entity(wait)
            job = replace(
                job, status="ready", phase="idle", active_trigger_id=None,
                last_completed_trigger_id=claim.trigger_id,
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
            dispatched.append(OD5aRecurringStep(
                claim.organization_id, claim.trigger_id,
                "advanced" if advanced else "complete",
                claim.epoch, job.logical_time, job.next_tick,
            ))

    backend.schedule_at(
        _time(due), commit_source_step,
        key=("od5a-recurring", claim.organization_id, claim.trigger_id, claim.epoch),
    )
    try:
        backend.run_until(_time(due))
    except ResourceCapacityError:
        if authority is None or not wait_requests or wait_attributes is None:
            raise
        # The source+physical transaction has rolled back. Only the exact
        # still-owned trigger can durably acknowledge backpressure in a NEW
        # fenced transaction. Another org/pool never waits on this job lock.
        with _job_transaction(store, claim.organization_id) as uow:
            source, job, lease = _assert_claim(
                uow, claim, _trusted_now(store, _wall_clock),
            )
            _assert_shared_binding(source, shared_pool_binding)
            current_owned = tuple(
                item for item in uow.resource_reservations()
                if item.resource_name in owned_names
            )
            if source.attributes != preflight_state or current_owned != preflight_reservations:
                raise StaleOD5aRunnerClaimError(
                    "OD-5a source/physical state changed before durable wait"
                )
            wait = uow.get_entity(_SHARED_WAIT_KIND, wait_id)
            _require_shared_wait_marker(
                lease, wait_id=wait_id, wait_exists=wait is not None,
            )
            if wait is not None:
                validate_wait(wait, epoch=claim.epoch)
            # Fencing must use the timestamp of THIS durable write, not the
            # previous snapshot's clock; a claim can expire between the two.
            stamp = _trusted_now(store, _wall_clock)
            _assert_claim(uow, claim, stamp)
            if wait is None:
                wait = Entity(
                    id=wait_id, entity_type=_SHARED_WAIT_KIND, state="waiting",
                    attributes={
                        **deepcopy(wait_attributes),
                        "origin_epoch": claim.epoch,
                        "last_claim_epoch": claim.epoch,
                    },
                )
                wait.touch(stamp)
                uow.save_entity(wait)
                lease.attributes = {
                    **lease.attributes, "physical_wait_id": wait_id,
                }
                lease.touch(stamp)
                uow.save_entity(lease)
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
    if len(dispatched) != 1:
        raise RuntimeError("native SimPy did not dispatch one authoritative OD-5a source action")
    return dispatched[0]


def run_od5a_recurring_tick(
    store: Persistence, *, job_id: str, owner_id: str,
    now: datetime, lease_duration: timedelta,
    trigger_id: str, recover: bool = False,
    _wall_clock: Callable[[], datetime] | None = None,
    shared_pool_binding: SharedPhysicalPoolBinding | None = None,
) -> OD5aRecurringStep:
    """One bounded recurring source step; ACK/certification are separate jobs.

    The external scheduler MUST persist and reuse trigger_id on retry; silently
    deriving it after a lost commit response would skip replay and execute
    another physical source step.
    """
    _require_wall_clock(now, lease_duration)
    if not trigger_id:
        raise ValueError("a stable external recurring trigger ID is required")
    with _job_transaction(store, job_id) as uow:
        source, job = _read_job(uow, job_id)
        _assert_shared_binding(source, shared_pool_binding)
        _assert_completed_released_lease(uow, job)
        if trigger_id is not None and trigger_id == job.last_completed_trigger_id:
            return OD5aRecurringStep(
                job_id, trigger_id, "replayed", 0,
                job.logical_time, job.next_tick,
            )
        if source.attributes["done"] and job.active_trigger_id is None:
            return OD5aRecurringStep(
                job_id, trigger_id,
                "complete", 0, job.logical_time, job.next_tick,
            )
    claim = claim_od5a_recurring_tick(
        store, job_id=job_id, owner_id=owner_id, now=now,
        lease_duration=lease_duration, trigger_id=trigger_id, recover=recover,
        _wall_clock=_wall_clock,
    )
    if claim is None:
        with _job_transaction(store, job_id) as uow:
            _, job = _read_job(uow, job_id)
            return OD5aRecurringStep(
                job_id, trigger_id,
                "replayed", 0, job.logical_time, job.next_tick,
            )
    return execute_od5a_claimed_tick(
        store, claim=claim, now=now, _wall_clock=_wall_clock,
        shared_pool_binding=shared_pool_binding,
    )


__all__ = [
    "SharedPhysicalPoolBinding", "SharedPhysicalGrantAuthority",
    "SharedPhysicalWaitSlotAdvice", "suggest_od5a_shared_wait_slots",
    "SharedPhysicalAlignedWaitAdvice", "suggest_od5a_aligned_wait_start",
    "OD5aRunnerClaim", "OD5aRecurringStep", "StaleOD5aRunnerClaimError",
    "initialize_od5a_recurring_job", "claim_od5a_recurring_tick",
    "execute_od5a_claimed_tick", "run_od5a_recurring_tick",
]
