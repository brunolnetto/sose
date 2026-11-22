"""#496 P496-B: attested, independently triggered component jobs.

Runs on memory, SQLite incremental and (with ``SOSE_TEST_POSTGRES_DSN``) PostgreSQL.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest

from sose.backends.simpy import SimPyBackend
from sose.composition.boundary import BoundaryConsumerRegistry
from sose.composition.job_observation import observe_organization_jobs
from sose.composition.organization import OrganizationDefinition, ProcessComponent, ScheduledJob
from sose.composition.organization_jobs import build_attested_component_jobs
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import builtin_process_manifests
from sose.jobs.model import OrganizationJobAttestation, SimulationJobState
from sose.jobs.persistent import PersistentJobRunner
from sose.jobs.runner import SimulationJob
from sose.persistence.codec import dumps, loads
from sose.persistence.memory import MemoryPersistence
from sose.persistence.ownership import FencedEnginePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
T0 = datetime(2026, 11, 22, 8, tzinfo=timezone.utc)
FULFILL, WAREHOUSE = "org:fulfill", "org:warehouse"


def definition(*, config_epoch: int = 0) -> OrganizationDefinition:
    return OrganizationDefinition(
        organization_id="org:p496b", version=1, config_epoch=config_epoch,
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


def _backend(origin):
    return SimPyBackend(origin=origin)


def build(store, spec=None, *, worker="worker-1"):
    jobs = build_attested_component_jobs(
        spec or definition(), store, catalog=builtin_catalog(),
        manifests=builtin_process_manifests(), consumers=BoundaryConsumerRegistry(),
        backend_factory=_backend, worker_id=worker,
    )
    return {job.scheduled.job_id: job for job in jobs}


def observe(store, spec=None):
    return observe_organization_jobs(
        spec or definition(), store, catalog=builtin_catalog(),
        manifests=builtin_process_manifests(), consumers=BoundaryConsumerRegistry(),
    )


ADAPTERS = ["memory", "sqlite-incremental",
            pytest.param("postgres", marks=pytest.mark.skipif(not DSN, reason="PostgreSQL"))]


@pytest.fixture(params=ADAPTERS)
def opener(request, tmp_path):
    """Return open(): a fresh handle on ONE durable store (memory: the same object)."""
    opened = []
    if request.param == "memory":
        store = MemoryPersistence()

        def open_store():
            return store
    elif request.param == "sqlite-incremental":
        def open_store():
            handle = SQLiteIncrementalPersistence(tmp_path / "org.sqlite3")
            opened.append(handle)
            return handle
    else:
        from sose.persistence.postgres import PostgresPersistence
        namespace = f"p496b_{uuid4().hex[:12]}"

        def open_store():
            handle = PostgresPersistence(DSN, namespace=namespace)
            opened.append(handle)
            return handle
    yield open_store
    for handle in opened:
        handle.close()


def test_component_jobs_run_independently_under_their_own_leases(opener):
    store = opener()
    jobs = build(store)
    for job in jobs.values():
        job.runner.initialize({})
    fulfill, warehouse = jobs[FULFILL].runner, jobs[WAREHOUSE].runner
    fulfill.run_trigger(trigger_id="f-1", triggered_at=T0)
    fulfill.run_trigger(trigger_id="f-2", triggered_at=T0)
    warehouse.run_trigger(trigger_id="w-1", triggered_at=T0)

    assert store.job_state(FULFILL).run_count == 2
    assert store.job_state(WAREHOUSE).run_count == 1
    # One job-scoped lease per component, claimed per call; no namespace-wide lease.
    assert store.scoped_writer_epoch(FULFILL) == 3
    assert store.scoped_writer_epoch(WAREHOUSE) == 2
    assert store.writer_epoch() == 0
    for job_id, job in jobs.items():
        assert store.job_state(job_id).organization_attestation == job.attestation
    evidence = observe(store)
    assert [(item.job_id, item.attested) for item in evidence.jobs] == [
        (FULFILL, True), (WAREHOUSE, True),
    ]


def test_attestation_survives_restart_and_the_run_continues(opener):
    first = opener()
    jobs = build(first)
    jobs[FULFILL].runner.initialize({})
    jobs[FULFILL].runner.run_trigger(trigger_id="f-1", triggered_at=T0)
    expected = jobs[FULFILL].attestation

    reopened = opener()
    state = reopened.job_state(FULFILL)
    assert state.organization_attestation == expected
    build(reopened, worker="worker-2")[FULFILL].runner.run_trigger(
        trigger_id="f-2", triggered_at=T0,
    )
    assert reopened.job_state(FULFILL).run_count == 2
    assert reopened.job_state(FULFILL).organization_attestation == expected
    assert observe(reopened).jobs[0].attested


def test_a_takeover_fences_only_its_own_component(opener):
    store = opener()
    jobs = build(store)
    for job in jobs.values():
        job.runner.initialize({})
    stale = store.claim_scoped_writer(
        FULFILL, "worker-1", expected_epoch=store.scoped_writer_epoch(FULFILL),
    )
    warehouse_before = store.scoped_writer_epoch(WAREHOUSE)
    build(store, worker="worker-2")[FULFILL].runner.run_trigger(trigger_id="f-1", triggered_at=T0)
    with pytest.raises(Exception, match="stale"):
        with FencedEnginePersistence(store, stale).transaction():
            pass
    assert store.scoped_writer_epoch(WAREHOUSE) == warehouse_before
    jobs[WAREHOUSE].runner.run_trigger(trigger_id="w-1", triggered_at=T0)
    assert store.job_state(WAREHOUSE).run_count == 1


def test_bindings_are_fail_closed_in_both_directions(opener):
    store = opener()
    catalog = builtin_catalog()
    # A plain job registered first cannot be adopted by the organization...
    plain = SimulationJob(job_id=FULFILL, definition=catalog.get("warehouse_fulfillment"),
                          persistence=store, backend_factory=_backend)
    PersistentJobRunner(job=plain, owner_id="worker-1").initialize({})
    epoch = store.scoped_writer_epoch(FULFILL)
    with pytest.raises(ValueError, match="attestation differs"):
        build(store)[FULFILL].runner.run_trigger(trigger_id="f-1", triggered_at=T0)
    # Refused before claiming: the correctly bound worker is not fenced out.
    assert store.scoped_writer_epoch(FULFILL) == epoch
    # ...and an attested job cannot be run by a plain runner or another binding.
    build(store)[WAREHOUSE].runner.initialize({})
    plain_warehouse = SimulationJob(
        job_id=WAREHOUSE, definition=catalog.get("warehouse_management"),
        persistence=store, backend_factory=_backend,
    )
    with pytest.raises(ValueError, match="attestation differs"):
        PersistentJobRunner(job=plain_warehouse, owner_id="worker-1").run_trigger(
            trigger_id="w-1", triggered_at=T0,
        )
    warehouse_epoch = store.scoped_writer_epoch(WAREHOUSE)
    with pytest.raises(ValueError, match="attestation differs"):
        build(store, definition(config_epoch=1))[WAREHOUSE].runner.run_trigger(
            trigger_id="w-1", triggered_at=T0,
        )
    assert store.scoped_writer_epoch(WAREHOUSE) == warehouse_epoch
    with pytest.raises(ValueError, match="another organizational binding"):
        observe(store, definition(config_epoch=1))
    assert store.job_state(WAREHOUSE).run_count == 0


def test_construction_refuses_an_attested_owner_that_would_not_write():
    store = MemoryPersistence()
    spec = replace(definition(), jobs=(ScheduledJob("job:fulfill", "fulfillment", "trigger:f"),))
    with pytest.raises(ValueError, match="must use its component's process owner"):
        build(store, spec)
    attestation = build(store)[FULFILL].attestation
    catalog = builtin_catalog()
    with pytest.raises(ValueError, match="attested owner must equal"):
        SimulationJob(job_id="other", definition=catalog.get("warehouse_fulfillment"),
                      persistence=store, backend_factory=_backend,
                      organization_attestation=attestation)
    with pytest.raises(ValueError, match="attested domain contradicts"):
        SimulationJob(job_id=FULFILL, definition=catalog.get("warehouse_management"),
                      persistence=store, backend_factory=_backend,
                      organization_attestation=attestation)
    with pytest.raises(ValueError, match="worker_id"):
        build(store, worker=" ")


def test_unattested_checkpoints_keep_their_bytes_and_attested_ones_round_trip():
    plain = SimulationJobState(
        job_id="j", domain_name="d", config_json="{}", config_revision=1,
        status="ready", initialized=True, logical_time=T0, next_tick=0,
    )
    assert "organization_attestation" not in dumps(plain)
    assert loads(dumps(plain)) == plain
    attested = replace(plain, organization_attestation=OrganizationJobAttestation(
        "org", "fingerprint", 0, "component", "j", "d",
    ))
    assert loads(dumps(attested)) == attested
    with pytest.raises(TypeError):
        replace(plain, organization_attestation={"organization_id": "org"})
    with pytest.raises(ValueError, match="config_epoch"):
        OrganizationJobAttestation("org", "fingerprint", -1, "component", "j", "d")
