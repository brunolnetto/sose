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
REFUSED_EXIT = 98
# Every chart each domain registers, so the snapshot attributes every transition
# of the composed run (declaring only the order and site charts left most of it,
# including the whole crashed trigger, outside the oracle).
FULFILLMENT_CHARTS = ("AllocationChart", "FulfillmentOrderChart",
                      "FulfillmentServiceTaskChart", "InventoryOccurrenceChart")
WAREHOUSE_CHARTS = ("DockChart", "ForkliftChart", "ShipmentChart", "TruckChart",
                    "WarehouseSiteChart")


def definition() -> OrganizationDefinition:
    return OrganizationDefinition(
        organization_id="org:equivalence", version=1,
        components=(
            ProcessComponent("fulfillment", "warehouse_fulfillment", FULFILL,
                             FULFILLMENT_CHARTS),
            ProcessComponent("warehouse", "warehouse_management", WAREHOUSE,
                             WAREHOUSE_CHARTS),
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


def durable_state(persistence) -> dict:
    """Every durable family the composed run writes, from one coherent view.

    Orders that legitimately differ between concurrent workers (the global event
    order across owners) are normalized; everything else is compared as stored,
    including entity versions/attributes, resource records, simulation positions
    and the full job checkpoints with their batch fields.
    """
    with persistence.read_snapshot() as view:
        events = view.events()
        return {
            "events_by_owner": {
                owner: [event for event in events if event.owner_id == owner]
                for owner in sorted({event.owner_id for event in events})
            },
            "entities": sorted(view.entities(), key=lambda e: (e.entity_type, e.id)),
            "resource_demands": view.resource_demands(),
            "resource_reservations": view.resource_reservations(),
            "resource_release_intents": view.resource_release_intents(),
            "simulation_positions": {
                owner: view.simulation_position(owner_id=owner)
                for owner in (FULFILL, WAREHOUSE)
            },
            "job_states": sorted(view.job_states(), key=lambda state: state.job_id),
            "scheduled_work": view.scheduled_work(),
        }


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
            try:
                component.run_trigger(
                    trigger_id=trigger_id(job_id, index), triggered_at=T0,
                    recover=index == recover_trigger,
                )
            except RuntimeError as error:
                if "requires explicit recovery" in str(error):
                    os._exit(REFUSED_EXIT)
                raise
