"""Explicit re-attestation of component jobs after an admitted definition change (#496, N1).

An attested component job records the admitted definition fingerprint and
``config_epoch`` it was registered under (P496-B). Any admitted change to the
organization changes the fingerprint, so its jobs refuse to run until they are
re-attested. ``reattest_component_jobs`` is that explicit, compare-and-swap step:

1. it admits the NEW definition;
2. it checks EVERY declared job before writing anything. A job is refused when it
   has a plain (unattested) checkpoint, when its component's identity changed
   (organization, component, owner or domain), when its recorded fingerprint and
   epoch are not exactly ``expected_previous``, or when an occurrence is in flight.
   A refusal at this stage writes nothing and moves no epoch;
3. it then rewrites each job's attestation, one job at a time, under that job's
   own scoped lease, re-reading the checkpoint inside the fenced transaction.
   ``job_id``, run counts and history are kept.

The rewrite is per job, not atomic across jobs. If it fails for one job (the job
changed after planning, a lease conflict, a database error), the jobs before it
stay re-attested and the rest keep the old binding; observation then refuses the
organization under either definition. The recovery is to run the same call again:
re-attested jobs report ``already_current`` and the rest are moved.

Claiming the lease fences a worker still running the job under the old binding;
that worker's runner is then refused and must be rebuilt from the new definition.
Jobs no longer declared by the new definition are left as they are.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from sose.jobs.model import SimulationJobState
from sose.persistence.ownership import FencedEnginePersistence

from .organization import OrganizationDefinition, validate_organization_admission
from .organization_jobs import component_job_attestation
from .physical_pools import ledger_for

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


class ReattestationError(ValueError):
    """The jobs cannot be re-attested without ambiguity; nothing was written."""


@dataclass(frozen=True, slots=True)
class ReattestReport:
    job_id: str
    component_id: str
    status: str  # "reattested", "already_current" or "absent"
    previous: tuple[str, int] | None
    current: tuple[str, int]
    scoped_writer_epoch: int | None


_IDENTITY = ("organization_id", "component_id", "owner_id", "domain_name")


def _plan(state: SimulationJobState | None, target, expected_previous) -> str:
    """Return the job's status, or raise when it cannot be re-attested."""
    if state is None:
        return "absent"
    recorded = state.organization_attestation
    if recorded is None:
        raise ReattestationError(f"job {state.job_id} is not an attested component job")
    if any(getattr(recorded, name) != getattr(target, name) for name in _IDENTITY):
        raise ReattestationError(
            f"job {state.job_id}: its component identity changed; re-attestation only "
            "moves a job to a new definition of the same component"
        )
    if recorded == target:
        return "already_current"
    if (recorded.definition_fingerprint, recorded.config_epoch) != expected_previous:
        raise ReattestationError(
            f"job {state.job_id} is attested to another definition than expected_previous"
        )
    if state.active_trigger_id is not None or state.active_batch_trigger_id is not None:
        raise ReattestationError(f"job {state.job_id} has an occurrence in flight")
    return "reattested"


def _require_arguments(expected_previous, worker_id) -> None:
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be nonempty")
    if (not isinstance(expected_previous, tuple) or len(expected_previous) != 2
            or not isinstance(expected_previous[0], str)
            or not isinstance(expected_previous[1], int)):
        raise TypeError("expected_previous must be (definition_fingerprint, config_epoch)")


def _plans(definition, fingerprint, persistence, expected_previous) -> list:
    """Check every declared job before anything is written; any refusal raises."""
    components = {component.component_id: component for component in definition.components}
    plans = []
    for scheduled in sorted(definition.jobs, key=lambda item: item.job_id):
        target = component_job_attestation(
            definition, fingerprint, scheduled, components[scheduled.component_id],
        )
        state = persistence.job_state(scheduled.job_id)
        plans.append((scheduled, target, state, _plan(state, target, expected_previous)))
    return plans


def _report(scheduled, state, status, current, epoch) -> ReattestReport:
    recorded = None if state is None else state.organization_attestation
    return ReattestReport(
        job_id=scheduled.job_id, component_id=scheduled.component_id, status=status,
        previous=(None if recorded is None
                  else (recorded.definition_fingerprint, recorded.config_epoch)),
        current=current, scoped_writer_epoch=epoch,
    )


def reattest_component_jobs(
    definition: OrganizationDefinition,
    persistence: "Persistence",
    *,
    expected_previous: tuple[str, int],
    catalog,
    manifests,
    consumers,
    worker_id: str,
    physical_ledger: bool = False,
) -> tuple[ReattestReport, ...]:
    """Move every declared, idle component job to ``definition``'s attestation."""
    _require_arguments(expected_previous, worker_id)
    fingerprint = validate_organization_admission(
        definition, catalog=catalog, manifests=manifests, consumers=consumers,
        physical_ledger=ledger_for(persistence, physical_ledger, definition),
    )
    current = (fingerprint, definition.config_epoch)
    reports = []
    for scheduled, target, state, status in _plans(
        definition, fingerprint, persistence, expected_previous,
    ):
        epoch = _rewrite(persistence, state, target, worker_id) if status == "reattested" else None
        reports.append(_report(scheduled, state, status, current, epoch))
    return tuple(reports)


def _rewrite(persistence, planned: SimulationJobState, target, worker_id: str) -> int:
    """Swap the attestation under the job's scoped lease; the checkpoint must be unchanged."""
    # Cheap check before claiming, so a job that already changed keeps its epoch.
    if persistence.job_state(planned.job_id) != planned:
        raise ReattestationError(
            f"job {planned.job_id} changed while being re-attested; its attestation "
            "was not changed (run the call again)"
        )
    lease = persistence.claim_scoped_writer(
        planned.job_id, worker_id, expected_epoch=persistence.scoped_writer_epoch(planned.job_id),
    )
    with FencedEnginePersistence(persistence, lease).transaction() as uow:
        current = uow.get_job_state(planned.job_id)
        if current != planned:
            raise ReattestationError(
                f"job {planned.job_id} changed while being re-attested; its attestation "
                "was not changed (run the call again)"
            )
        uow.save_job_state(replace(current, organization_attestation=target))
    return lease.epoch
