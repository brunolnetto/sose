"""Process entry points for composed-organization equivalence tests (#496).

Spawned child processes import this module, so it holds the organization
definition, the per-component workload and the crash hook.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os

from sose.backends.simpy import SimPyBackend
from sose.composition.boundary import BoundaryConsumerRegistry
from sose.composition.organization import OrganizationDefinition, ProcessComponent, ScheduledJob
from sose.composition.organization_jobs import build_attested_component_jobs
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import builtin_process_manifests

T0 = datetime(2026, 11, 22, 8, tzinfo=timezone.utc)
FULFILL, WAREHOUSE = "org:fulfill", "org:warehouse"
TRIGGERS = 4
CRASH_EXIT = 97


def definition() -> OrganizationDefinition:
    return OrganizationDefinition(
        organization_id="org:equivalence", version=1,
        components=(
            ProcessComponent("fulfillment", "warehouse_fulfillment", FULFILL,
                             ("FulfillmentOrderChart",)),
            ProcessComponent("warehouse", "warehouse_management", WAREHOUSE,
                             ("WarehouseSiteChart",)),
        ),
        jobs=(
            ScheduledJob(FULFILL, "fulfillment", "trigger:fulfill"),
            ScheduledJob(WAREHOUSE, "warehouse", "trigger:warehouse"),
        ),
    )


def admission() -> dict:
    return dict(catalog=builtin_catalog(), manifests=builtin_process_manifests(),
                consumers=BoundaryConsumerRegistry())


def _backend(origin):
    return SimPyBackend(origin=origin)


def runner(persistence, job_id: str, worker: str):
    jobs = build_attested_component_jobs(
        definition(), persistence, backend_factory=_backend, worker_id=worker, **admission(),
    )
    return {job.scheduled.job_id: job for job in jobs}[job_id].runner


def trigger_id(job_id: str, index: int) -> str:
    return f"{job_id}:trigger-{index}"


def run_component(persistence, job_id: str, worker: str, *, start: int = 0) -> None:
    """Register the job (idempotently) and run its triggers from ``start``."""
    component = runner(persistence, job_id, worker)
    component.initialize({})
    for index in range(start, TRIGGERS):
        component.run_trigger(trigger_id=trigger_id(job_id, index), triggered_at=T0)


@contextmanager
def _crash_after(persistence, commits: int):
    """Exit the process right after the ``commits``-th committed transaction."""
    original = persistence.transaction
    seen = {"commits": 0}

    @contextmanager
    def counting(*args, **kwargs):
        with original(*args, **kwargs) as uow:
            yield uow
        seen["commits"] += 1
        if seen["commits"] == commits:
            os._exit(CRASH_EXIT)

    persistence.transaction = counting
    try:
        yield
    finally:
        persistence.transaction = original


def component_worker(dsn: str, namespace: str, job_id: str, worker: str,
                     crash_trigger: int | None = None, crash_commits: int = 2,
                     recover_trigger: int | None = None) -> None:
    """Child process: run one component; optionally die mid-occurrence.

    ``recover_trigger`` names the occurrence a predecessor left open: the runtime
    only resumes an interrupted batch when the caller asks for recovery explicitly.
    """
    from sose.persistence.postgres import PostgresPersistence

    with PostgresPersistence(dsn, namespace=namespace) as persistence:
        component = runner(persistence, job_id, worker)
        component.initialize({})
        for index in range(TRIGGERS):
            if index == crash_trigger:
                with _crash_after(persistence, crash_commits):
                    component.run_trigger(trigger_id=trigger_id(job_id, index), triggered_at=T0)
                raise AssertionError("the crash hook did not fire inside the occurrence")
            component.run_trigger(
                trigger_id=trigger_id(job_id, index), triggered_at=T0,
                recover=index == recover_trigger,
            )
