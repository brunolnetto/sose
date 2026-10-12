"""One-step recurring OD-5a *reference* orchestration with bounded causal work.

This is deliberately NOT a production SimPy-backed recurring SimulationJob.
It combines the authoritative standalone reducer with the committed-source
boundary reconciler as a falsification/control harness for #485. It never
serializes SimPy objects and never synthesizes physical events for an ACK.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sose.persistence.base import Persistence

from .od5a_durable import advance_od5a_durable
from .od5a_reconciler import (
    OD5aReconcileStep,
    reconcile_od5a_boundary_step,
)


@dataclass(frozen=True, slots=True)
class OD5aReferenceOccurrence:
    organization_id: str
    action: Literal[
        "source_advanced", "published", "claimed", "acknowledged",
        "materialized", "certified", "blocked", "complete",
    ]
    event_id: str | None = None
    retry_at: datetime | None = None


def advance_od5a_reference_occurrence(
    store: Persistence, *, job_id: str, owner_id: str, now: datetime,
    lease_duration: timedelta,
) -> OD5aReferenceOccurrence:
    """Perform at most one committed physical-source or boundary transition.

    The existing read-side reconciler always gets first refusal, preventing a
    producer from outrunning already persisted work after recovery. If there
    are no outstanding physical events, the authoritative reference reducer
    advances exactly one causal action in its own transactional UoW.

    This is a deliberately bounded benchmark scaffold, not evidence that
    native ephemeral SimPy yet runs the durable recurring production adapter.
    """
    step: OD5aReconcileStep = reconcile_od5a_boundary_step(
        store, job_id=job_id, owner_id=owner_id,
        now=now, lease_duration=lease_duration,
    )
    if step.action == "idle":
        advanced = advance_od5a_durable(store, job_id=job_id)
        if not advanced:
            # A completed reducer with no outstanding boundary work has no
            # semantic transition left to commit.
            return OD5aReferenceOccurrence(job_id, "complete")
        return OD5aReferenceOccurrence(job_id, "source_advanced")
    return OD5aReferenceOccurrence(
        organization_id=job_id, action=step.action,
        event_id=step.event_id, retry_at=step.retry_at,
    )


__all__ = ["OD5aReferenceOccurrence", "advance_od5a_reference_occurrence"]
