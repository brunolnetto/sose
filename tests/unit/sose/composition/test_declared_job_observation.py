"""#496 W3b: real checkpoints are not interchangeable with declared job IDs."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry
from sose.composition.job_observation import observe_organization_jobs
from sose.composition.organization import OrganizationDefinition, ProcessComponent, ScheduledJob
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import builtin_process_manifests
from sose.jobs.model import SimulationJobState
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def specification():
    return OrganizationDefinition(
        organization_id="org:job-observation",
        version=1,
        components=(
            ProcessComponent("fulfillment", "warehouse_fulfillment", "org:fulfill",
                             ("FulfillmentOrderChart",)),
            ProcessComponent("warehouse", "warehouse_management", "org:warehouse",
                             ("WarehouseSiteChart",)),
        ),
        jobs=(
            ScheduledJob("job:fulfill", "fulfillment", "trigger:fulfill"),
            ScheduledJob("job:warehouse", "warehouse", "trigger:warehouse"),
        ),
    )


def checkpoint(job_id="job:fulfill", domain="warehouse_fulfillment", *,
               status="ready", next_tick=2):
    return SimulationJobState(
        job_id=job_id, domain_name=domain, config_json="{}",
        config_revision=1, status=status, initialized=True,
        logical_time=NOW, next_tick=next_tick, run_count=1,
        last_completed_trigger_id="trigger:completed",
    )


def observe(spec, store):
    return observe_organization_jobs(
        spec, store, catalog=builtin_catalog(),
        manifests=builtin_process_manifests(),
        consumers=BoundaryConsumerRegistry(),
    )


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_persisted_and_declared_only_checkpoints_are_distinct_across_restart(adapter, tmp_path):
    path = tmp_path / "job-observation.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    definition = specification()
    try:
        with store.transaction() as uow:
            uow.save_job_state(checkpoint())
            uow.save_job_state(checkpoint("job:unrelated", "warehouse_management"))
        before = tuple(store.job_states())
        first = observe(definition, store)
        assert first == observe(definition, store)
        assert tuple(store.job_states()) == before
        assert first.definition_fingerprint == definition.fingerprint()
        assert len(first.jobs) == 2
        first_job, second_job = first.jobs
        assert first_job.job_id == "job:fulfill"
        assert first_job.declared_owner_id == "org:fulfill"
        assert first_job.checkpoint_present
        assert first_job.expected_domain == "warehouse_fulfillment"
        assert (first_job.status, first_job.next_tick, first_job.run_count) == ("ready", 2, 1)
        assert first_job.last_completed_trigger_id == "trigger:completed"
        assert second_job.job_id == "job:warehouse"
        assert second_job.declared_owner_id == "org:warehouse"
        assert not second_job.checkpoint_present
        assert second_job.status is None
        assert second_job.next_tick is None
        assert second_job.logical_time is None
        assert "job:unrelated" not in {item.job_id for item in first.jobs}
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            assert observe(definition, reopened) == first


@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_persisted_wrong_domain_cannot_be_claimed_as_component_job(adapter, tmp_path):
    path = tmp_path / "wrong-domain.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    try:
        with store.transaction() as uow:
            uow.save_job_state(checkpoint(domain="warehouse_management"))
        before = tuple(store.job_states())
        with pytest.raises(ValueError, match="job domain contradicts persisted"):
            observe(specification(), store)
        assert tuple(store.job_states()) == before
    finally:
        if adapter == "sqlite":
            store.close()


def test_repeated_snapshot_job_identity_is_rejected_without_writes():
    class DuplicateJobs:
        def __init__(self):
            self.count = 0

        def job_states(self):
            self.count += 1
            return (checkpoint(), replace(checkpoint(), next_tick=3))

    store = DuplicateJobs()
    with pytest.raises(ValueError, match="duplicate persisted organizational job identity"):
        observe(specification(), store)
    assert store.count == 1


def test_declared_jobs_with_no_checkpoints_never_imply_runtime_execution():
    class EmptyJobs:
        def __init__(self):
            self.count = 0

        def job_states(self):
            self.count += 1
            return ()

    store = EmptyJobs()
    observed = observe(specification(), store)
    assert all(not item.checkpoint_present for item in observed.jobs)
    assert all(item.run_count is None for item in observed.jobs)
    assert store.count == 1



@pytest.mark.parametrize("adapter", ("memory", "sqlite"))
def test_read_only_job_observation_preserves_naive_writer_checkpoint(adapter, tmp_path):
    """A legal naive domain start clock must survive W3b inspection and restart."""
    path = tmp_path / "naive-job-observation.sqlite"
    store = MemoryPersistence() if adapter == "memory" else SQLitePersistence(path)
    naive_time = datetime(2026, 10, 19, 12)
    persisted = replace(checkpoint(), logical_time=naive_time)
    try:
        with store.transaction() as uow:
            uow.save_job_state(persisted)
        before = tuple(store.job_states())
        evidence = observe(specification(), store)
        assert evidence.jobs[0].logical_time == naive_time
        assert evidence.jobs[0].logical_time.tzinfo is None
        assert evidence.jobs[0].checkpoint_present
        assert tuple(store.job_states()) == before
    finally:
        if adapter == "sqlite":
            store.close()
    if adapter == "sqlite":
        with SQLitePersistence(path) as reopened:
            assert observe(specification(), reopened) == evidence
