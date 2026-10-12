"""Per-organization recurring OD-5a source step driven by ephemeral native SimPy.

First bounded #485 bridge only: SimPy dispatches a committed source-step
callback, while the authoritative OD-5a reducer still arbitrates physical
reservations and causal transitions. This does NOT yet make native SimPy
resource scheduling the authoritative production operational mechanism,
nor does it drain the read-side boundary reconciler (#484).

A scoped durable lease and SimulationJobState checkpoint fence a worker. The
physical event, resource ledger and job checkpoint commit in ONE transaction.
No SimPy Environment, Process, Event or callback is persisted.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from json import dumps
from typing import Callable, Literal

from sose.backends.simpy import SimPyBackend
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity
from sose.jobs.model import SimulationJobState
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_admission import OD5aCompiledArm
from .od5a_durable import (
    _KIND, _ORIGIN, _time, _advance_od5a_in_uow,
    initialize_od5a_durable,
)
from .od5a_mechanisms import OD5aWorkItem


_LEASE_KIND = "od5a_runner_lease"
_DOMAIN = "od5a"

@contextmanager
def _job_transaction(store: Persistence, job_id: str):
    """Serialize same-job PostgreSQL OLTP writes, not unrelated organizations.

    PostgresPersistence has last-writer-wins record updates. Its native
    business_resource_guard acquires an exclusive session advisory lock on
    precisely this organization's identity and releases it after the UoW.
    SQLite retains its own BEGIN IMMEDIATE transaction semantics.
    """
    guard = getattr(store, "business_resource_guard", None)
    if callable(guard):
        with guard("od5a:recurring:" + job_id):
            with store.transaction() as uow:
                yield uow
    else:
        with store.transaction() as uow:
            yield uow




class StaleOD5aRunnerClaimError(RuntimeError):
    """The worker cannot advance with a missing, expired or superseded epoch."""


@dataclass(frozen=True, slots=True)
class OD5aRunnerClaim:
    organization_id: str
    trigger_id: str
    owner_id: str
    epoch: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class OD5aRecurringStep:
    organization_id: str
    trigger_id: str
    action: Literal["advanced", "complete", "replayed"]
    epoch: int
    logical_time: datetime
    logical_tick: int


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
    if (
        job.domain_name != _DOMAIN
        or job.config_json != dumps(identity, sort_keys=True, separators=(",", ":"))
        or job.bootstrap_state != identity
        or not job.initialized
        or job.config_revision != 1
        or job.next_tick != source.attributes["step"]
        or job.logical_time != _time(source.attributes["clock"])
    ):
        raise ValueError("OD-5a recurring checkpoint contradicts authoritative organization")
    return source, job


def _require_wall_clock(now: datetime, lease_duration: timedelta | None = None) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("OD-5a worker time must be timezone-aware")
    if lease_duration is not None and lease_duration <= timedelta(0):
        raise ValueError("OD-5a runner lease must be positive")


def _trusted_now(
    store: Persistence, wall_clock: Callable[[], datetime] | None,
) -> datetime:
    """Take an independent trusted timestamp AFTER obtaining the job lock.

    PostgreSQL uses its database server clock across contending hosts. Other
    stores use the local UTC system clock. The optional injected clock is
    strictly a deterministic test seam; never allow caller-supplied 'now'
    to influence fencing or the lease-expiry calculation.
    """
    if wall_clock is not None:
        observed = wall_clock()
    else:
        db_clock = getattr(store, "trusted_utc_now", None)
        observed = db_clock() if callable(db_clock) else datetime.now(timezone.utc)
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


def _assert_claim(
    uow: UnitOfWork, claim: OD5aRunnerClaim, now: datetime,
) -> tuple[Entity, SimulationJobState, Entity]:
    source, job = _read_job(uow, claim.organization_id)
    lease = uow.get_entity(_LEASE_KIND, claim.organization_id)
    if (
        lease is None
        or lease.state != "claimed"
        or lease.attributes.get("trigger_id") != claim.trigger_id
        or lease.updated_at is None
        or now < lease.updated_at
        or _lease_expiry(lease) <= lease.updated_at
        or job.active_trigger_id != claim.trigger_id
        or job.status != "running"
        or job.phase != "advance"
        or lease.attributes.get("owner_id") != claim.owner_id
        or lease.attributes.get("epoch") != claim.epoch
        or _lease_expiry(lease) != claim.lease_expires_at
        or now >= claim.lease_expires_at
    ):
        raise StaleOD5aRunnerClaimError(
            f"OD-5a runner claim superseded or expired: {claim.organization_id}"
        )
    return source, job, lease


def initialize_od5a_recurring_job(
    store: Persistence, *, job_id: str, arm: OD5aCompiledArm,
    items: tuple[OD5aWorkItem, ...],
) -> SimulationJobState:
    """Bind the registered immutable pilot and one durable per-organization job."""
    guard = getattr(store, "business_resource_guard", None)
    if callable(guard):
        with guard("od5a:recurring:" + job_id):
            initialize_od5a_durable(store, job_id=job_id, arm=arm, items=items)
            return _register_od5a_job(store, job_id=job_id)
    initialize_od5a_durable(store, job_id=job_id, arm=arm, items=items)
    return _register_od5a_job(store, job_id=job_id)


def _register_od5a_job(store: Persistence, *, job_id: str) -> SimulationJobState:
    # Call under the per-organization guard when PostgreSQL is used.
    with store.transaction() as uow:
        source = uow.get_entity(_KIND, job_id)
        assert source is not None
        prior = uow.get_job_state(job_id)
        if prior is not None:
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
        if job.last_completed_trigger_id == trigger_id:
            return None
        expected_id = _next_trigger_id(job)
        if trigger_id != expected_id:
            raise ValueError("OD-5a runner trigger ID is not the next deterministic step")
        if source.attributes["done"]:
            if job.active_trigger_id is not None:
                raise ValueError("completed organization retains active runner trigger")
            return None
        if job.status == "paused":
            raise ValueError("OD-5a recurring job is paused")
        if job.active_trigger_id not in (None, trigger_id):
            raise StaleOD5aRunnerClaimError("OD-5a runner has a different unfinished trigger")

        existing = uow.get_entity(_LEASE_KIND, job_id)
        epoch = 1
        if existing is not None:
            epoch = existing.attributes.get("epoch", 0)
            if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
                raise ValueError("invalid durable OD-5a runner epoch")
            expiry = _lease_expiry(existing)
            previous_owner = existing.attributes.get("owner_id")
            if existing.state not in {"claimed", "released"}:
                raise ValueError("OD-5a runner lease has invalid durable lifecycle")
            if previous_owner is None and existing.state != "released":
                raise ValueError("OD-5a runner lease is claimed without an owner")
            if previous_owner is not None and existing.state != "claimed":
                raise ValueError("OD-5a runner lease owner contradicts released state")
            if previous_owner is not None and trusted_now < expiry:
                if (
                    previous_owner == owner_id
                    and job.active_trigger_id == trigger_id
                ):
                    return OD5aRunnerClaim(
                        job_id, trigger_id, owner_id, epoch, expiry,
                    )
                raise StaleOD5aRunnerClaimError("active OD-5a runner lease belongs to another worker")
            if job.active_trigger_id is not None and not recover:
                raise StaleOD5aRunnerClaimError("unfinished source trigger requires explicit recovery")
            epoch += 1
        elif job.active_trigger_id is not None:
            raise StaleOD5aRunnerClaimError("unfinished source trigger lost authoritative lease")

        expires_at = trusted_now + lease_duration
        lease = existing or Entity(
            id=job_id, entity_type=_LEASE_KIND, state="released",
        )
        lease.state = "claimed"
        lease.attributes = {
            "owner_id": owner_id,
            "epoch": epoch,
            "expires_at": expires_at.isoformat(),
            "trigger_id": trigger_id,
        }
        lease.touch(trusted_now)
        uow.save_entity(lease)
        uow.save_job_state(replace(
            job, status="running", phase="advance", active_trigger_id=trigger_id,
            last_triggered_at=trusted_now, last_error=None,
        ))
        return OD5aRunnerClaim(job_id, trigger_id, owner_id, epoch, expires_at)


def execute_od5a_claimed_tick(
    store: Persistence, *, claim: OD5aRunnerClaim, now: datetime,
    _wall_clock: Callable[[], datetime] | None = None,
) -> OD5aRecurringStep:
    """Use a fresh ephemeral SimPy dispatch to commit exactly one source step.

    A kill after claim or inside callback leaves the uncommitted action
    retriable with a higher epoch; a kill after COMMIT leaves the checkpoint
    and the physical event inseparable.
    """
    _require_wall_clock(now)
    with _job_transaction(store, claim.organization_id) as uow:
        source, _, _ = _assert_claim(
            uow, claim, _trusted_now(store, _wall_clock),
        )
        actions = source.attributes["actions"]
        due = (
            min(actions, key=lambda x: (x["at"], x["seq"]))["at"]
            if actions else source.attributes["clock"]
        )
        next_action = (
            (min(actions, key=lambda x: (x["at"], x["seq"]))["at"],
             min(actions, key=lambda x: (x["at"], x["seq"]))["seq"],
             min(actions, key=lambda x: (x["at"], x["seq"]))["kind"])
            if actions else None
        )
    dispatched: list[OD5aRecurringStep] = []
    backend = SimPyBackend(origin=_ORIGIN)

    def commit_source_step() -> None:
        with _job_transaction(store, claim.organization_id) as uow:
            # Re-read the trusted clock AFTER the lock. Neither invocation
            # time nor the worker host's clock can extend/stale the fence.
            commit_at = _trusted_now(store, _wall_clock)
            source, job, lease = _assert_claim(uow, claim, commit_at)
            actions = source.attributes["actions"]
            actual = min(actions, key=lambda x: (x["at"], x["seq"])) if actions else None
            actual_id = (
                (actual["at"], actual["seq"], actual["kind"])
                if actual is not None else None
            )
            if actual_id != next_action:
                raise StaleOD5aRunnerClaimError("OD-5a scheduled step changed before SimPy dispatch")
            advanced = _advance_od5a_in_uow(uow, job_id=claim.organization_id)
            committed = uow.get_entity(_KIND, claim.organization_id)
            assert committed is not None
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
            lease.attributes = {**lease.attributes, "owner_id": None}
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
    backend.run_until(_time(due))
    if len(dispatched) != 1:
        raise RuntimeError("native SimPy did not dispatch one authoritative OD-5a source action")
    return dispatched[0]


def run_od5a_recurring_tick(
    store: Persistence, *, job_id: str, owner_id: str,
    now: datetime, lease_duration: timedelta,
    trigger_id: str, recover: bool = False,
    _wall_clock: Callable[[], datetime] | None = None,
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
    )


__all__ = [
    "OD5aRunnerClaim", "OD5aRecurringStep", "StaleOD5aRunnerClaimError",
    "initialize_od5a_recurring_job", "claim_od5a_recurring_tick",
    "execute_od5a_claimed_tick", "run_od5a_recurring_tick",
]
