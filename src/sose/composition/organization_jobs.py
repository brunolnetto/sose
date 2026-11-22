"""Attested, independently triggered component jobs of an admitted organization (#496, P496-B).

Every ``ScheduledJob`` of an admitted ``OrganizationDefinition`` becomes its own
``SimulationJob`` behind its own job-scoped writer lease (``PersistentJobRunner``).
There is no organization-wide clock, global lock or super-StateChart: components
are triggered independently and never fence each other.

Each job's checkpoint carries an immutable ``OrganizationJobAttestation``
(organization, admitted definition fingerprint, config epoch, component, owner,
domain), written when the job is first registered. A runner with another binding,
or none, is refused, so a checkpoint can be told apart from an unrelated job that
merely reuses a declared ``job_id``.

Scope: the attestation is an identity binding written by trusted in-process code
through the job's own fenced transaction. It is not a protected writer-role/key
boundary; persistence-level authority remains #545.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sose.jobs.model import OrganizationJobAttestation
from sose.jobs.persistent import PersistentJobRunner
from sose.jobs.runner import SimulationJob, scheduled_trigger_id

from .organization import (
    OrganizationDefinition,
    ProcessComponent,
    ScheduledJob,
    validate_organization_admission,
)

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class AttestedComponentJob:
    """One declared job, its component, the binding it carries, and its runner."""

    scheduled: ScheduledJob
    component: ProcessComponent
    attestation: OrganizationJobAttestation
    runner: PersistentJobRunner

    def trigger_id_for(self, scheduled_for: datetime) -> str:
        """The stable trigger identity of one scheduled slot, in this job's namespace.

        ``ScheduledJob.trigger_namespace`` is unique within the admitted definition,
        so independently scheduled components never share a trigger identity.
        """
        return scheduled_trigger_id(self.scheduled.trigger_namespace, scheduled_for)

    def run_scheduled_trigger(
        self, *, scheduled_for: datetime, ticks: int | None = None, recover: bool = False,
    ):
        """Run the occurrence for one scheduled slot.

        Replaying a completed slot returns its record without running it again. A
        slot left open by a dead worker needs ``recover=True``, as for any batch.
        This is the only scheduling path for a component job: the job's own
        ``job_id``-based ``run_scheduled_trigger`` refuses attested jobs.
        """
        return self.runner.run_trigger(
            trigger_id=self.trigger_id_for(scheduled_for), ticks=ticks,
            triggered_at=scheduled_for, recover=recover,
        )


def component_job_attestation(
    definition: OrganizationDefinition,
    fingerprint: str,
    scheduled: ScheduledJob,
    component: ProcessComponent,
) -> OrganizationJobAttestation:
    """The binding a declared job's checkpoint must carry."""
    return OrganizationJobAttestation(
        organization_id=definition.organization_id,
        definition_fingerprint=fingerprint,
        config_epoch=definition.config_epoch,
        component_id=component.component_id,
        owner_id=component.owner_id,
        domain_name=component.domain,
    )


def build_attested_component_jobs(
    definition: OrganizationDefinition,
    persistence: "Persistence",
    *,
    catalog,
    manifests,
    consumers,
    backend_factory: Callable,
    worker_id: str,
    ticks_per_trigger: int = 1,
) -> tuple[AttestedComponentJob, ...]:
    """Admit ``definition``, then build one attested runner per declared job.

    A ``SimulationJob`` writes as Engine owner ``job_id``, so a declared job must use
    its component's process ``owner_id`` as its ``job_id``; otherwise the attested
    owner would not be the owner that actually writes, and the job is refused.
    ``worker_id`` names the lease holder (the process running the jobs), not an
    organizational owner.
    """
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be nonempty")
    fingerprint = validate_organization_admission(
        definition, catalog=catalog, manifests=manifests, consumers=consumers,
    )
    components = {component.component_id: component for component in definition.components}
    built = []
    for scheduled in sorted(definition.jobs, key=lambda item: item.job_id):
        component = components[scheduled.component_id]
        if scheduled.job_id != component.owner_id:
            raise ValueError(
                f"component job {scheduled.job_id!r} must use its component's process "
                f"owner {component.owner_id!r} as job_id: a SimulationJob writes as its job_id"
            )
        attestation = component_job_attestation(definition, fingerprint, scheduled, component)
        job = SimulationJob(
            job_id=scheduled.job_id,
            definition=catalog.get(component.domain),
            persistence=persistence,
            backend_factory=backend_factory,
            ticks_per_trigger=ticks_per_trigger,
            organization_attestation=attestation,
        )
        built.append(AttestedComponentJob(
            scheduled=scheduled, component=component, attestation=attestation,
            runner=PersistentJobRunner(job=job, owner_id=worker_id),
        ))
    return tuple(built)
