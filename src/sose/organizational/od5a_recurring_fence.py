"""Durable runner-lease fencing of the recurring OD-5a source step.

Checkpoint/lease consistency checks and claim validation, split out of
od5a_recurring (which re-exports every name). Check order and messages are
part of the fencing contract and are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from json import dumps
from typing import Callable

from sose.core.identity import deterministic_id
from sose.core.resource_reservations import ResourceConflictError
from sose.domain.entity import Entity
from sose.jobs.model import SimulationJobState
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_durable import _KIND, _time


_LEASE_KIND = "od5a_runner_lease"
_DOMAIN = "od5a"


class StaleOD5aRunnerClaimError(RuntimeError):
    """The worker cannot advance with a missing, expired or superseded epoch."""


@dataclass(frozen=True, slots=True)
class OD5aRunnerClaim:
    organization_id: str
    trigger_id: str
    owner_id: str
    epoch: int
    lease_expires_at: datetime


def _config(source: Entity) -> dict[str, str]:
    return {
        "pilot_identity": source.attributes["identity"],
        "model_spec_hash": source.attributes["model_spec_hash"],
        "arm_id": source.attributes["arm_id"],
    }


def _read_job(
    uow: UnitOfWork, job_id: str,
) -> tuple[Entity, SimulationJobState]:
    source = uow.get_entity(_KIND, job_id)
    job = uow.get_job_state(job_id)
    if source is None or job is None:
        raise KeyError(f"OD-5a recurring job is not initialized: {job_id}")
    identity = _config(source)
    if _job_config_contradicts(job, identity) or _job_progress_contradicts(job, source, job_id):
        raise ValueError("OD-5a recurring checkpoint contradicts authoritative organization")
    return source, job


def _job_config_contradicts(job: SimulationJobState, identity: dict[str, str]) -> bool:
    return (
        job.domain_name != _DOMAIN
        or job.config_json != dumps(identity, sort_keys=True, separators=(",", ":"))
        or job.bootstrap_state != identity
        or not job.initialized
        or job.config_revision != 1
    )


def _job_progress_contradicts(job: SimulationJobState, source: Entity, job_id: str) -> bool:
    return bool(
        job.next_tick != source.attributes["step"]
        or type(job.run_count) is not int
        # The last real physical action marks the source done in the same
        # transaction that advances its step and the recurring run_count.
        # A subsequent completion query consumes no trigger or source step.
        or job.run_count != source.attributes["step"]
        # The reducer commits terminal source truth with the last real step.
        # A terminal physical organization can never hold a new active claim.
        # Enforce this in the common checkpoint reader (before any source
        # replay OR downstream boundary reconciliation).
        or (source.attributes["done"] and job.active_trigger_id is not None)
        # Bind the committed trigger identity to the authoritative run count.
        # Equality with a released lease alone is insufficient: forged job
        # and lease IDs could otherwise authorize an invalid replay.
        or job.last_completed_trigger_id != (
            deterministic_id("od5a-recurring-tick", job_id, job.run_count)
            if job.run_count else None
        )
        or job.logical_time != _time(source.attributes["clock"])
    )


def _require_wall_clock(now: datetime, lease_duration: timedelta | None = None) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("OD-5a worker time must be timezone-aware")
    if lease_duration is not None and lease_duration <= timedelta(0):
        raise ValueError("OD-5a runner lease must be positive")


def _trusted_now(
    store: Persistence, wall_clock: Callable[[], datetime] | None,
) -> datetime:
    """Take an independent trusted timestamp AFTER obtaining the job lock.

    PostgreSQL ALWAYS uses its database server clock across contending hosts.
    The optional injected clock is permitted ONLY on non-PostgreSQL reference
    stores; callers cannot override a production database's fencing clock.
    Never allow caller-supplied 'now' to influence lease expiry.
    """
    db_clock = getattr(store, "trusted_utc_now", None)
    if callable(db_clock):
        # Production PostgreSQL fencing MUST use the authoritative server
        # clock, with no caller-controlled escape hatch. Even a private test
        # injection could permit a fast host to steal a live epoch.
        if wall_clock is not None:
            raise ValueError("PostgreSQL trusted lease time cannot be overridden")
        observed = db_clock()
    elif wall_clock is not None:
        # Test-only deterministic time is available on reference adapters.
        observed = wall_clock()
    else:
        observed = datetime.now(timezone.utc)
    _require_wall_clock(observed)
    return observed.astimezone(timezone.utc)


def _next_trigger_id(job: SimulationJobState) -> str:
    return deterministic_id("od5a-recurring-tick", job.job_id, job.run_count + 1)


def _lease_expiry(source: Entity) -> datetime:
    value = source.attributes.get("expires_at")
    if not isinstance(value, str):
        raise ValueError("OD-5a runner lease has invalid expiry representation")
    result = datetime.fromisoformat(value)
    _require_wall_clock(result)
    return result



def _assert_completed_released_lease(
    uow: UnitOfWork, job: SimulationJobState,
) -> None:
    """A completed step without its released fence cannot be safely replayed.

    A newer active trigger legitimately has a *claimed* lease instead;
    only idle/completed trigger history requires the released record.
    """
    if job.last_completed_trigger_id is None or job.active_trigger_id is not None:
        return
    if job.status != "ready" or job.phase != "idle":
        raise ValueError("OD-5a completed checkpoint lifecycle contradicts idle released lease")
    lease = uow.get_entity(_LEASE_KIND, job.job_id)
    if (
        _released_lease_mismatch(lease, job)
        or _bad_epoch(lease.attributes.get("epoch"), 1, job.run_count)
        or lease.attributes.get("physical_wait_id") is not None
    ):
        raise ValueError("OD-5a completed trigger lacks released lease history")
    if _lease_expiry(lease) <= lease.updated_at:
        raise ValueError("OD-5a completed trigger lacks released lease history")



def _bad_epoch(epoch: object, *minimums: int) -> bool:
    """Not a strict int, or below any minimum (checked in the given order)."""
    return (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or any(epoch < minimum for minimum in minimums)
    )


def _released_lease_mismatch(lease: Entity | None, job: SimulationJobState) -> bool:
    return (
        lease is None
        or lease.state != "released"
        or lease.attributes.get("owner_id") is not None
        or lease.attributes.get("trigger_id") != job.last_completed_trigger_id
        or job.run_count < 1
        or lease.updated_at is None
    )


def _assert_active_claimed_lease(
    uow: UnitOfWork, job: SimulationJobState,
) -> None:
    """Prove claimed source fencing *history* before downstream mutations.

    A claimed worker may have died or its lease expired, yet independently
    committed physical events still need delivery. Therefore validate its
    immutable claim identity and lifecycle without requiring a live owner or
    extending the expired lease. Actual source execution still calls
    `_assert_claim` with the trusted PostgreSQL clock.
    """
    if job.active_trigger_id is None:
        return
    lease = uow.get_entity(_LEASE_KIND, job.job_id)
    if (
        job.status != "running"
        or job.phase != "advance"
        or job.active_trigger_id != _next_trigger_id(job)
        or _claimed_lease_mismatch(lease, job.active_trigger_id)
        or _bad_epoch(lease.attributes.get("epoch"), job.run_count + 1)
        or _foreign_wait_marker(lease, job)
    ):
        raise ValueError("OD-5a active lease contradicts runner claim history")
    try:
        expiry = _lease_expiry(lease)
    except (TypeError, ValueError) as exc:
        raise ValueError("OD-5a active lease contradicts runner claim expiry") from exc
    if expiry <= lease.updated_at:
        raise ValueError("OD-5a active lease contradicts runner claim expiry")


def _claimed_lease_mismatch(lease: Entity | None, trigger_id: str | None) -> bool:
    return (
        lease is None
        or lease.state != "claimed"
        or lease.attributes.get("trigger_id") != trigger_id
        or not isinstance(lease.attributes.get("owner_id"), str)
        or not lease.attributes.get("owner_id")
        or lease.updated_at is None
    )


def _foreign_wait_marker(lease: Entity, job: SimulationJobState) -> bool:
    return (
        lease.attributes.get("physical_wait_id") is not None
        and lease.attributes["physical_wait_id"] != deterministic_id(
            "od5a-shared-capacity-wait", job.job_id, job.active_trigger_id,
        )
    )


def _assert_claim(
    uow: UnitOfWork, claim: OD5aRunnerClaim, now: datetime,
) -> tuple[Entity, SimulationJobState, Entity]:
    source, job = _read_job(uow, claim.organization_id)
    lease = uow.get_entity(_LEASE_KIND, claim.organization_id)
    if lease is not None and lease.state == "claimed":
        stored_epoch = lease.attributes.get("epoch")
        if _bad_epoch(stored_epoch, 1):
            raise ValueError("invalid durable OD-5a runner epoch")
        if stored_epoch < job.run_count + 1:
            raise ValueError("OD-5a claimed lease epoch predates next run")
    if (
        _claim_lease_window_mismatch(lease, claim, now)
        or job.active_trigger_id != claim.trigger_id
        or job.status != "running"
        or job.phase != "advance"
        or _claim_identity_mismatch(lease, claim, now)
    ):
        raise StaleOD5aRunnerClaimError(
            f"OD-5a runner claim superseded or expired: {claim.organization_id}"
        )
    return source, job, lease


def _trigger_claimable(source: Entity, job: SimulationJobState, trigger_id: str) -> bool:
    """False when the trigger is already committed or the organization is done."""
    if job.last_completed_trigger_id == trigger_id:
        return False
    expected_id = _next_trigger_id(job)
    if trigger_id != expected_id:
        raise ValueError("OD-5a runner trigger ID is not the next deterministic step")
    if source.attributes["done"]:
        if job.active_trigger_id is not None:
            raise ValueError("completed organization retains active runner trigger")
        return False
    if job.status == "paused":
        raise ValueError("OD-5a recurring job is paused")
    if job.active_trigger_id not in (None, trigger_id):
        raise StaleOD5aRunnerClaimError("OD-5a runner has a different unfinished trigger")
    return True


def _validated_prior_lease(
    existing: Entity, job: SimulationJobState, job_id: str,
) -> tuple[int, datetime, object, object]:
    """Validate the OLD fence: (epoch, expiry, owner, pending physical wait ID)."""
    epoch = existing.attributes.get("epoch", 0)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise ValueError("invalid durable OD-5a runner epoch")
    expiry = _lease_expiry(existing)
    previous_owner = existing.attributes.get("owner_id")
    _require_prior_lease_lifecycle(existing, previous_owner)
    # Validate the OLD physical fence before a recovering worker can
    # replace it. Otherwise an expired forged lease is laundered by
    # the new epoch, hiding its inconsistent causal trigger or clock.
    if existing.updated_at is None or expiry <= existing.updated_at:
        raise ValueError("OD-5a runner lease expiry contradicts claim timestamp")
    pending_physical_wait_id = existing.attributes.get("physical_wait_id")
    if pending_physical_wait_id is not None and _pending_wait_contradicts(
        existing, job, job_id, pending_physical_wait_id,
    ):
        raise ResourceConflictError("pending physical wait marker contradicts claimed trigger")
    if existing.state == "claimed":
        _require_claimed_prior_lease(existing, job, epoch)
    if existing.state == "released":
        _require_released_prior_lease(existing, job)
    return epoch, expiry, previous_owner, pending_physical_wait_id


def _require_prior_lease_lifecycle(existing: Entity, previous_owner: object) -> None:
    if existing.state not in {"claimed", "released"}:
        raise ValueError("OD-5a runner lease has invalid durable lifecycle")
    if existing.state == "claimed" and (
        not isinstance(previous_owner, str) or not previous_owner
    ):
        raise ValueError("OD-5a runner lease is claimed without an owner (nonempty string required)")
    if previous_owner is not None and existing.state != "claimed":
        raise ValueError("OD-5a runner lease owner contradicts released state")


def _pending_wait_contradicts(
    existing: Entity, job: SimulationJobState, job_id: str, pending_physical_wait_id: object,
) -> bool:
    return (
        existing.state != "claimed"
        or job.active_trigger_id is None
        or pending_physical_wait_id != deterministic_id(
            "od5a-shared-capacity-wait", job_id, job.active_trigger_id,
        )
    )


def _require_claimed_prior_lease(
    existing: Entity, job: SimulationJobState, epoch: int,
) -> None:
    if epoch < job.run_count + 1:
        raise ValueError("OD-5a claimed lease epoch predates next run")
    if job.status != "running" or job.phase != "advance":
        raise ValueError("OD-5a claimed lease contradicts job lifecycle")
    if (
        job.active_trigger_id is None
        or existing.attributes.get("trigger_id") != job.active_trigger_id
    ):
        raise ValueError("OD-5a runner lease trigger contradicts active job")


def _require_released_prior_lease(existing: Entity, job: SimulationJobState) -> None:
    # A released fence is created only when an authorized source
    # step and its job checkpoint commit together. A fabricated
    # pre-first-tick released epoch must never be laundered by
    # incrementing it into a new valid claim.
    if job.run_count < 1 or not job.last_completed_trigger_id:
        raise ValueError("released lease requires completed trigger history")
    if job.active_trigger_id is not None:
        raise ValueError("released OD-5a runner lease contradicts active job trigger")
    if existing.attributes.get("trigger_id") != job.last_completed_trigger_id:
        raise ValueError("released OD-5a runner lease trigger contradicts completed job")


def _claim_lease_window_mismatch(
    lease: Entity | None, claim: OD5aRunnerClaim, now: datetime,
) -> bool:
    return (
        lease is None
        or lease.state != "claimed"
        or lease.attributes.get("trigger_id") != claim.trigger_id
        or lease.updated_at is None
        or now < lease.updated_at
        or _lease_expiry(lease) <= lease.updated_at
    )


def _claim_identity_mismatch(lease: Entity, claim: OD5aRunnerClaim, now: datetime) -> bool:
    return (
        lease.attributes.get("owner_id") != claim.owner_id
        or lease.attributes.get("epoch") != claim.epoch
        or _lease_expiry(lease) != claim.lease_expires_at
        or now >= claim.lease_expires_at
    )
