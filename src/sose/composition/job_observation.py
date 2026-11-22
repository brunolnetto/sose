"""Read-side observation of declared component job checkpoints (#496 W3b).

This does not claim a job ran merely because its ScheduledJob is admitted.
A checkpoint written by an attested component job (``organization_jobs``, P496-B)
carries an immutable ``OrganizationJobAttestation``; ``attested`` reports whether it
is exactly the binding this admitted definition expects. A checkpoint without one is
still only a job_id + domain match: *observational*, not execution authority.
No shared clock, global transaction or mutable super-StateChart is created.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .organization import OrganizationDefinition, validate_organization_admission
from .organization_jobs import component_job_attestation

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class DeclaredJobCheckpoint:
    component_id: str
    declared_owner_id: str
    expected_domain: str
    job_id: str
    checkpoint_present: bool
    attested: bool
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
    checkpoints remain explicit None fields; a mismatched domain, a duplicated
    persisted identity, or an attestation to another binding fails closed. Without
    an attestation the declared owner is *only configuration*.
    """
    fingerprint = validate_organization_admission(
        definition, catalog=catalog, manifests=manifests, consumers=consumers,
    )
    components = {component.component_id: component for component in definition.components}
    observed = _observed_declared_checkpoints(definition, persistence, components)
    output = [
        _declared_job_checkpoint(
            job, components[job.component_id], observed.get(job.job_id),
            component_job_attestation(definition, fingerprint, job, components[job.component_id]),
        )
        for job in sorted(definition.jobs, key=lambda item: item.job_id)
    ]
    return OrganizationJobCheckpointEvidence(
        organization_id=definition.organization_id,
        definition_fingerprint=fingerprint,
        version=definition.version,
        config_epoch=definition.config_epoch,
        jobs=tuple(output),
    )


def _observed_declared_checkpoints(definition, persistence, components) -> dict:
    """Persisted checkpoints of the declared jobs only, keyed by job_id."""
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
    return observed


_CHECKPOINT_FIELDS = (
    "status", "phase", "logical_time", "next_tick", "run_count",
    "active_trigger_id", "last_completed_trigger_id",
)


def _declared_job_checkpoint(job, component, checkpoint, expected) -> DeclaredJobCheckpoint:
    """Missing checkpoints stay explicit: every observed field is None."""
    attestation = None if checkpoint is None else checkpoint.organization_attestation
    if attestation is not None and attestation != expected:
        raise ValueError("persisted job checkpoint is attested to another organizational binding")
    observed = {
        name: (getattr(checkpoint, name) if checkpoint else None)
        for name in _CHECKPOINT_FIELDS
    }
    return DeclaredJobCheckpoint(
        component_id=component.component_id,
        declared_owner_id=component.owner_id,
        expected_domain=component.domain,
        job_id=job.job_id,
        checkpoint_present=checkpoint is not None,
        attested=attestation == expected,
        **observed,
    )
