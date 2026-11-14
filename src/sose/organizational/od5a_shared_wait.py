"""Read-only shared-capacity wait advice for blocked recurring OD-5a steps (#477).

Split out of od5a_recurring, which re-exports every name. No function here
writes an event, source action, checkpoint, wait record or grant.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from json import dumps

from sose.core.identity import deterministic_id
from sose.core.resource_reservations import ResourceConflictError, TemporalReservation
from sose.domain.entity import Entity
from sose.persistence.base import Persistence

from .od5a_durable import _time, _transition
from .od5a_native_arbiter import _PhysicalStepPreview
from .od5a_shared_wait_alignment import _aligned_physical_start
from .od5a_recurring_fence import (
    _LEASE_KIND,
    _SHARED_WAIT_KIND,
    _assert_active_claimed_lease,
    _job_transaction,
    _read_job,

)
from .od5a_shared_authority import (
    SharedPhysicalGrantAuthority,
    SharedPhysicalPoolBinding,
    _assert_shared_binding,
)


@dataclass(frozen=True, slots=True)
class SharedPhysicalWaitSlotAdvice:
    """One physical request's advisory start, never a committed joint grant."""

    reservation_id: str
    instance_id: str
    original_start: datetime
    duration: timedelta
    suggested_start: datetime | None
    # Immutable provenance of the actual speculative native request. The
    # aligned read must never synthesize a substitute causal parent.
    original_request: TemporalReservation
    wait_id: str
    trigger_id: str
    claim_epoch: int
    source_digest: str


@dataclass(frozen=True, slots=True)
class SharedPhysicalAlignedWaitAdvice:
    """Versioned joint-start candidate; deliberately NOT a grant or a plan commit."""

    advice_version: int
    wait_id: str
    trigger_id: str
    claim_epoch: int
    source_digest: str
    binding_fingerprint: str
    not_before: datetime
    latest_start: datetime
    proposed_start: datetime | None
    slots: tuple[SharedPhysicalWaitSlotAdvice, ...]


def _source_wait_digest(source_state: dict) -> str:
    """Canonical typed snapshot binding a durable wait to its source truth."""
    return sha256(dumps(
        source_state, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _validate_shared_wait_record(
    wait: Entity, *, expected: dict | None, epoch: int,
) -> None:
    """Require complete v2 source provenance on reads AND committed retries.

    A missing schema tag or digest cannot be interpreted as legacy: a
    corrupted v2 wait could otherwise be downgraded by deleting both fields.
    Pre-v2 historical waits must be quarantined, not re-admitted to physical
    execution by an unauthenticated fallback.
    """
    attributes = wait.attributes
    origin = attributes.get("origin_epoch")
    observed = attributes.get("last_claim_epoch")
    digest = attributes.get("source_digest")
    version = attributes.get("wait_schema_version")
    if (
        wait.state != "waiting"
        or expected is None
        or _wait_epochs_invalid(origin, observed, epoch)
        or type(version) is not int or version != 2
        or not isinstance(digest, str) or len(digest) != 64
        or dumps(attributes, sort_keys=True, separators=(",", ":"), allow_nan=False)
        != dumps({
            **expected,
            "origin_epoch": origin,
            "last_claim_epoch": observed,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False)
    ):
        raise ResourceConflictError(
            "shared wait record contradicts native source request or runner epoch"
        )


def _wait_epochs_invalid(origin: object, observed: object, epoch: int) -> bool:
    return (
        type(origin) is not int or type(observed) is not int
        or not 1 <= origin <= observed <= epoch
    )


def _require_shared_wait_marker(
    lease: Entity, *, wait_id: str, wait_exists: bool,
) -> None:
    """Tie a blocked source trigger to an INDEPENDENT durable lease record.

    The marker is saved atomically with the initial wait and carried across
    recovery epochs. Deleting the wait entity must not restore the source's
    right to behave like a fresh, never-blocked physical request.
    """
    marker = lease.attributes.get("physical_wait_id")
    if marker is not None and marker != wait_id:
        raise ResourceConflictError("pending physical wait marker contradicts source trigger")
    if wait_exists and marker != wait_id:
        raise ResourceConflictError("pending physical wait lacks its lease marker")
    if not wait_exists and marker is not None:
        raise ResourceConflictError("pending physical wait references missing shared wait")


def suggest_od5a_shared_wait_slots(
    store: Persistence, *, job_id: str,
    shared_pool_binding: SharedPhysicalPoolBinding,
    not_before: datetime, latest_start: datetime,
) -> tuple[SharedPhysicalWaitSlotAdvice, ...]:
    """Read-only bounded hints for a *persisted* blocked source step (#477).

    Reconstruct the exact pending grants through the operational reducer on
    a disposable view, verify the typed durable wait and its live trigger
    identity, then query each named physical instance's authoritative ledger.
    No event, source action, checkpoint, wait record, or grant is changed.
    Multiple individual hints are NOT a joint reservation or a coherent
    reschedule. A separate versioned scheduler must revalidate all requests
    and original causal parents inside its own fenced commit.
    """
    # Initialize DDL outside the job transaction; ledger construction has
    # its own namespace-scoped bootstrap guard, unlike the advisory query.
    authority = SharedPhysicalGrantAuthority(
        store, shared_pool_binding, job_id, bootstrap_ledger=False,
    )
    with _job_transaction(store, job_id) as uow:
        source, job = _read_job(uow, job_id)
        _assert_shared_binding(source, shared_pool_binding)
        trigger_id, lease = _current_claimed_wait_trigger(uow, job, job_id)
        next_action, wait_id, wait = _pending_shared_wait(
            uow, source, lease, job_id, trigger_id,
        )
        planned, matched = _preview_shared_grants(uow, source, job_id, authority)
        _validate_shared_wait_record(
            wait,
            expected={
                "trigger_id": trigger_id,
                "binding_fingerprint": shared_pool_binding.fingerprint(),
                "source_action": next_action,
                "due_at": _time(next_action["at"]).isoformat(),
                "requests": [
                    {
                        "resource_name": grant.resource_name,
                        "reservation_id": grant.reservation_id,
                        "request_id": grant.request_id,
                        "instance_id": slot[1],
                    }
                    for grant, slot in matched
                ],
                "wait_schema_version": 2,
                "source_digest": _source_wait_digest(source.attributes),
            },
            epoch=lease.attributes["epoch"],
        )
        if not matched:
            raise ResourceConflictError("durable shared wait has no physical requests")
        provenance = {
            "wait_id": wait_id, "trigger_id": trigger_id,
            "claim_epoch": lease.attributes["epoch"],
            "source_digest": wait.attributes["source_digest"],
        }
        return tuple(
            _wait_slot_hint(
                authority, grant, slot, planned, job_id, provenance,
                not_before=not_before, latest_start=latest_start,
            )
            for grant, slot in matched
        )


def _current_claimed_wait_trigger(uow, job, job_id: str) -> tuple[str, Entity]:
    try:
        _assert_active_claimed_lease(uow, job)
    except ValueError as exc:
        raise ResourceConflictError(
            "shared wait lacks valid claimed runner lease history"
        ) from exc
    trigger_id = job.active_trigger_id
    lease = uow.get_entity(_LEASE_KIND, job_id)
    if (
        trigger_id is None or job.status != "running" or job.phase != "advance"
        or lease is None or lease.state != "claimed"
        or lease.attributes.get("trigger_id") != trigger_id
        or type(lease.attributes.get("epoch")) is not int
        or lease.attributes["epoch"] < 1
    ):
        raise ResourceConflictError("shared wait lacks a current claimed trigger")
    return trigger_id, lease


def _pending_shared_wait(
    uow, source: Entity, lease: Entity, job_id: str, trigger_id: str,
) -> tuple[dict, str, Entity]:
    actions = source.attributes["actions"]
    if not actions:
        raise ResourceConflictError("shared wait lacks the original pending action")
    next_action = dict(min(actions, key=lambda a: (a["at"], a["seq"])))
    wait_id = deterministic_id("od5a-shared-capacity-wait", job_id, trigger_id)
    wait = uow.get_entity(_SHARED_WAIT_KIND, wait_id)
    if wait is None:
        raise ResourceConflictError("pending physical wait references missing shared wait")
    _require_shared_wait_marker(lease, wait_id=wait_id, wait_exists=True)
    return next_action, wait_id, wait


def _preview_shared_grants(
    uow, source: Entity, job_id: str, authority: SharedPhysicalGrantAuthority,
) -> tuple[dict, list]:
    # Only the source's own local leases are input to this speculative
    # transition; another organization cannot forge the grant identities.
    owned_names = {
        f"{job_id}/{slot}" for slot in source.attributes["resources"]
    }
    owned = tuple(
        item for item in uow.resource_reservations()
        if item.resource_name in owned_names
    )
    preview = _PhysicalStepPreview(owned)
    planned = deepcopy(source.attributes)
    _transition(preview, planned, job_id)
    matched = sorted((
        (grant, slot)
        for grant in preview.grants
        if (slot := authority._slot(grant.resource_name)) is not None
    ), key=lambda pair: pair[0].reservation_id)
    return planned, matched


def _wait_slot_hint(
    authority: SharedPhysicalGrantAuthority, grant, slot: tuple[str, str],
    planned: dict, job_id: str, provenance: dict, *,
    not_before: datetime, latest_start: datetime,
) -> SharedPhysicalWaitSlotAdvice:
    local_name, instance = slot
    activity = planned["active"].get(local_name)
    if activity is None or activity.get("reservation_id") != grant.reservation_id:
        raise ResourceConflictError("wait request lacks planned physical activity")
    ends = [
        action["at"] for action in planned["actions"]
        if action.get("kind") == "execution_done"
        and action.get("resource") == local_name
    ]
    if len(ends) != 1 or _time(ends[0]) <= grant.acquired_at:
        raise ResourceConflictError("wait request lacks exact service duration")
    physical = TemporalReservation(
        reservation_id=authority._id(grant.reservation_id),
        address=authority.binding.pool.address(instance_id=instance),
        owner_id=job_id,
        start_at=grant.acquired_at,
        end_at=_time(ends[0]),
        causation_id=activity["causes"][0],
    )
    suggested = authority.ledger.suggest_next_start(
        physical, not_before=not_before, latest_start=latest_start,
    )
    return SharedPhysicalWaitSlotAdvice(
        reservation_id=grant.reservation_id, instance_id=instance,
        original_start=physical.start_at,
        duration=physical.end_at - physical.start_at,
        suggested_start=suggested, original_request=physical,
        **provenance,
    )


def suggest_od5a_aligned_wait_start(
    store: Persistence, *, job_id: str,
    shared_pool_binding: SharedPhysicalPoolBinding,
    not_before: datetime, latest_start: datetime,
) -> SharedPhysicalAlignedWaitAdvice:
    """Construct a typed, versioned, *nonbinding* shared-wait start candidate.

    Verifies the durable source/claimed trigger/wait/lease provenance through
    the existing read-only source bridge. Finds a common start across the exact
    original physical requests instead of taking max(individual hints).
    It never edits a source action, commits a reservation, changes an epoch,
    issues a scheduling Command, or promises admission under concurrent use.

    A subsequent versioned scheduling protocol must retain original exogenous
    arrival/event identity and wait costs and acquire fresh grants in its own
    fenced source+pool transaction. This v1 advice is not that protocol.
    """
    slots = suggest_od5a_shared_wait_slots(
        store, job_id=job_id, shared_pool_binding=shared_pool_binding,
        not_before=not_before, latest_start=latest_start,
    )
    identities = {
        (slot.wait_id, slot.trigger_id, slot.claim_epoch, slot.source_digest)
        for slot in slots
    }
    if len(identities) != 1:
        raise ResourceConflictError("aligned physical hints have conflicting source provenance")
    wait_id, trigger_id, epoch, source_digest = next(iter(identities))
    ledger = SharedPhysicalGrantAuthority(
        store, shared_pool_binding, job_id, bootstrap_ledger=False,
    ).ledger
    proposed = _aligned_physical_start(
        ledger, tuple(slot.original_request for slot in slots),
        not_before=not_before, latest_start=latest_start,
    )
    return SharedPhysicalAlignedWaitAdvice(
        advice_version=1,
        wait_id=wait_id, trigger_id=trigger_id, claim_epoch=epoch,
        source_digest=source_digest,
        binding_fingerprint=shared_pool_binding.fingerprint(),
        not_before=not_before, latest_start=latest_start,
        proposed_start=proposed, slots=slots,
    )
