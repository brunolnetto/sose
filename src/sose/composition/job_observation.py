"""Read-side observation of declared component job checkpoints (#496 W3b).

This does not claim a job ran merely because its ScheduledJob is admitted.
A SimulationJobState has no immutable organizational owner attestation; an
exact job_id + domain match remains *observational*, not execution authority.
No shared clock, global transaction or mutable super-StateChart is created.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .organization import OrganizationDefinition, validate_organization_admission

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class DeclaredJobCheckpoint:
    component_id: str
    declared_owner_id: str
    expected_domain: str
    job_id: str
    checkpoint_present: bool
    status: str | None
    phase: str | None
    logical_time: datetime | None
    next_tick: int | None
    run_count: int | None
    active_trigger_id: str | None
    last_completed_trigger_id: str | None


@dataclass(frozen=True, slots=True)
class OrganizationJobCheckpointEvidence:
    organization_id: str
    definition_fingerprint: str
    version: int
    config_epoch: int
    jobs: tuple[DeclaredJobCheckpoint, ...]


def observe_organization_jobs(
    definition: OrganizationDefinition,
    persistence: "Persistence",
    *,
    catalog,
    manifests,
    consumers,
) -> OrganizationJobCheckpointEvidence:
    """Read ONE job-state tuple after validating the exact admitted topology.

    No inference from a declared job to an executed job. Missing persisted
    checkpoints remain explicit None fields; a mismatched domain or duplicated
    persisted identity fails closed. The declared owner is *only configuration*
    because the underlying SimulationJobState has no immutable owner_id.
    """
    fingerprint = validate_organization_admission(
        definition, catalog=catalog, manifests=manifests, consumers=consumers,
    )
    components = {component.component_id: component for component in definition.components}
    declared = {job.job_id: job for job in definition.jobs}
    observed = {}
    for checkpoint in persistence.job_states():
        if checkpoint.job_id not in declared:
            continue  # Unrelated jobs cannot become constituent evidence.
        if checkpoint.job_id in observed:
            raise ValueError("duplicate persisted organizational job identity")
        component = components[declared[checkpoint.job_id].component_id]
        if checkpoint.domain_name != component.domain:
            raise ValueError("declared organizational job domain contradicts persisted checkpoint")
        # Job writers allow naive as well as timezone-aware local logical
        # clocks. Observation must preserve the committed value verbatim,
        # not impose a stronger new timezone contract on old checkpoints.
        observed[checkpoint.job_id] = checkpoint

    output = []
    for job in sorted(definition.jobs, key=lambda item: item.job_id):
        component = components[job.component_id]
        checkpoint = observed.get(job.job_id)
        output.append(DeclaredJobCheckpoint(
            component_id=component.component_id,
            declared_owner_id=component.owner_id,
            expected_domain=component.domain,
            job_id=job.job_id,
            checkpoint_present=checkpoint is not None,
            status=checkpoint.status if checkpoint else None,
            phase=checkpoint.phase if checkpoint else None,
            logical_time=checkpoint.logical_time if checkpoint else None,
            next_tick=checkpoint.next_tick if checkpoint else None,
            run_count=checkpoint.run_count if checkpoint else None,
            active_trigger_id=checkpoint.active_trigger_id if checkpoint else None,
            last_completed_trigger_id=(
                checkpoint.last_completed_trigger_id if checkpoint else None
            ),
        ))
    return OrganizationJobCheckpointEvidence(
        organization_id=definition.organization_id,
        definition_fingerprint=fingerprint,
        version=definition.version,
        config_epoch=definition.config_epoch,
        jobs=tuple(output),
    )
