"""Bounded integrated OD-5a occurrence: SimPy source + authoritative boundary.

One invocation commits at most one causal transition. Reconciliation of
ALREADY COMMITTED physical events takes priority over a fresh source action.
The two directions retain separate transactions, durable lease/epoch owners
and independent failure windows; a source callback never fabricates a delivery.

This is not native SimPy physical resource-arbitration promotion or a
scientific OD-5a confirmatory freeze (which remains NOT_ADMITTED).
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sose.core.identity import deterministic_id
from sose.persistence.base import Persistence
from sose.persistence.ownership import FencedEnginePersistence

from .od5a_recurring import (
    _assert_active_claimed_lease, _assert_completed_released_lease,
    _read_job, _require_wall_clock, run_od5a_recurring_tick,
)
from .od5a_reconciler import reconcile_od5a_boundary_step


IntegratedAction = Literal[
    "source_advanced", "source_complete", "source_replayed",
    "published", "claimed", "acknowledged", "materialized",
    "certified", "blocked", "complete",
]


@dataclass(frozen=True, slots=True)
class OD5aIntegratedOccurrence:
    organization_id: str
    action: IntegratedAction
    event_id: str | None = None
    source_trigger_id: str | None = None
    logical_tick: int | None = None
    retry_at: datetime | None = None


@contextmanager
def _organization_guard(store: Persistence, job_id: str):
    """Short, per-organization operation fence, never a global scheduler.

    PostgreSQL's scoped advisory session lock is reentrant, so the source
    runner and standalone reducer use the SAME key. This protects the
    reconciler plan -> one commit and the source branch decision without
    collapsing independent publication, ACK and certificate transactions.
    SQLite retains each independently authoritative BEGIN IMMEDIATE UoW.
    """
    guard = getattr(store, "business_resource_guard", None)
    if callable(guard):
        with guard("od5a:recurring:" + job_id):
            yield
    else:
        yield


def advance_od5a_integrated_occurrence(
    store: Persistence, *, job_id: str, owner_id: str,
    source_trigger_id: str, now: datetime, lease_duration: timedelta,
    recover: bool = False,
) -> OD5aIntegratedOccurrence:
    """At most one committed source or boundary phase for an organization.

    The caller MUST persist the source trigger and retry that same ID after
    a lost COMMIT response. Boundary recovery is independently identified
    by the committed physical event and the reconciler's delivery fence.
    """
    if not job_id or not owner_id or not source_trigger_id:
        raise ValueError("integrated recurrence requires organization, owner and source trigger")
    _require_wall_clock(now, lease_duration)
    with _organization_guard(store, job_id):
        # Corrupt source/checkpoint history must not be laundered by an ACK
        # or a certificate before the source admission check can reject it.
        with store.transaction() as uow:
            _, job = _read_job(uow, job_id)
            # Completed physical effects cannot be certified from a missing
            # or forged released source fence. This validation precedes every
            # read-side publication, claim, ACK, observation and certificate.
            _assert_completed_released_lease(uow, job)
            _assert_active_claimed_lease(uow, job)

        # Scoped PostgreSQL boundary transactions use a *shared* namespace
        # causal lock plus their own organization lock, not the legacy
        # exclusive namespace-wide lock. Claim a fresh per-organization
        # writer epoch under the same physical-source guard so no concurrent
        # worker can observe an unfenced publication/ACK/certificate.
        boundary_store = store
        scoped_epoch = getattr(store, "scoped_writer_epoch", None)
        scoped_claim = getattr(store, "claim_scoped_writer", None)
        if callable(scoped_epoch) and callable(scoped_claim):
            scope = "od5a:boundary:" + job_id
            lease = scoped_claim(
                scope, owner_id, expected_epoch=scoped_epoch(scope),
                correlation_id=job_id,
            )
            boundary_store = FencedEnginePersistence(store, lease)
        step = reconcile_od5a_boundary_step(
            boundary_store, job_id=job_id, owner_id=owner_id,
            now=now, lease_duration=lease_duration,
        )
        if step.action == "complete":
            # Reconciliation has drained the source DAG. Preserve the
            # externally persisted trigger's lost-response replay semantics:
            # replaying the LAST committed source occurrence is distinct from
            # probing completion using the NEXT deterministic occurrence.
            if source_trigger_id == job.last_completed_trigger_id:
                return OD5aIntegratedOccurrence(
                    organization_id=job_id, action="source_replayed",
                    source_trigger_id=source_trigger_id,
                    logical_tick=job.next_tick,
                )
            if source_trigger_id != deterministic_id(
                "od5a-recurring-tick", job_id, job.run_count + 1,
            ):
                raise ValueError(
                    "OD-5a terminal trigger ID contradicts deterministic progress"
                )
        if step.action != "idle":
            return OD5aIntegratedOccurrence(
                organization_id=job_id, action=step.action,
                event_id=step.event_id, retry_at=step.retry_at,
            )
        source = run_od5a_recurring_tick(
            store, job_id=job_id, owner_id=owner_id,
            now=now, lease_duration=lease_duration,
            trigger_id=source_trigger_id, recover=recover,
        )
        return OD5aIntegratedOccurrence(
            organization_id=job_id, action="source_" + source.action,
            source_trigger_id=source.trigger_id,
            logical_tick=source.logical_tick,
        )


__all__ = ["OD5aIntegratedOccurrence", "advance_od5a_integrated_occurrence"]
