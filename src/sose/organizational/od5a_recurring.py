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

from dataclasses import replace
from datetime import datetime, timedelta
from json import dumps
from typing import Callable

from sose.domain.entity import Entity
from sose.jobs.model import SimulationJobState
from sose.jobs.registration import job_registration_guard
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_admission import OD5aCompiledArm
from .od5a_durable import _KIND, _time, initialize_od5a_durable
from .od5a_mechanisms import OD5aWorkItem
from .od5a_recurring_execution import OD5aRecurringStep, execute_od5a_claimed_tick
from .od5a_recurring_fence import (
    _DOMAIN,
    _LEASE_KIND,
    OD5aRunnerClaim,
    StaleOD5aRunnerClaimError,
    _assert_completed_released_lease,
    _config,
    _job_transaction,
    _read_job,
    _require_wall_clock,
    _trigger_claimable,
    _trusted_now,
    _validated_prior_lease,
)
from .od5a_shared_authority import (
    SharedPhysicalGrantAuthority,
    SharedPhysicalPoolBinding,
    _assert_shared_binding,
)
from .od5a_shared_wait import (
    SharedPhysicalAlignedWaitAdvice,
    SharedPhysicalWaitSlotAdvice,
    suggest_od5a_aligned_wait_start,
    suggest_od5a_shared_wait_slots,
)

# Former module-level internals, kept importable from their historical home
# after the split (tests and sibling modules reference several of them).
from .od5a_durable import _ORIGIN, _advance_od5a_in_uow, _transition  # noqa: F401,E402
from .od5a_native_arbiter import (  # noqa: F401,E402
    _PhysicalStepPreview,
    _RecurringNativePhysicalArbiter,
)
from .od5a_recurring_fence import (  # noqa: F401,E402
    _SHARED_WAIT_KIND,
    _assert_active_claimed_lease,
    _assert_claim,
    _lease_expiry,
    _next_trigger_id,
)
from .od5a_shared_wait import (  # noqa: F401,E402
    _aligned_physical_start,
    _require_shared_wait_marker,
    _source_wait_digest,
    _validate_shared_wait_record,
)

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
