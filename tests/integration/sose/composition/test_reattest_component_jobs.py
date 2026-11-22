"""#496 N1: explicit, compare-and-swap re-attestation after an admitted definition change.

Runs on memory, SQLite incremental and (with ``SOSE_TEST_POSTGRES_DSN``) PostgreSQL.
"""
# The ``opener`` fixture is imported and then requested by name: ruff reads that as
# a redefinition, pytest as fixture injection.
# ruff: noqa: F811
from __future__ import annotations

from dataclasses import replace

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry
from sose.composition.organization import OrganizationDefinition, ProcessComponent, ScheduledJob
from sose.composition.organization_reattest import ReattestationError, reattest_component_jobs
from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import builtin_process_manifests
from sose.jobs.persistent import PersistentJobRunner
from sose.jobs.runner import SimulationJob
from sose.persistence.ownership import FencedEnginePersistence
from tests.integration.sose.composition.test_attested_component_jobs import (  # noqa: F401
    FULFILL, T0, WAREHOUSE, _backend, build, definition, observe, opener,
)

V0, V1 = definition(), definition(config_epoch=1)
PREVIOUS = (V0.fingerprint(), 0)


def reattest(store, spec=V1, *, expected=PREVIOUS):
    return reattest_component_jobs(
        spec, store, expected_previous=expected, catalog=builtin_catalog(),
        manifests=builtin_process_manifests(), consumers=BoundaryConsumerRegistry(),
        worker_id="operator",
    )


def started(store, *, run=True):
    jobs = build(store, V0)
    for job in jobs.values():
        job.runner.initialize({})
    if run:
        jobs[FULFILL].runner.run_trigger(trigger_id="f-1", triggered_at=T0)
    return jobs


def _snapshot(store):
    return {job_id: (store.job_state(job_id), store.scoped_writer_epoch(job_id))
            for job_id in (FULFILL, WAREHOUSE)}


def test_reattested_jobs_continue_under_the_new_definition(opener):
    store = opener()
    started(store)
    before = store.job_state(FULFILL)
    with pytest.raises(ValueError, match="attestation differs"):
        build(store, V1)[FULFILL].runner.run_trigger(trigger_id="f-2", triggered_at=T0)

    reports = reattest(store)
    assert [(r.job_id, r.status, r.previous) for r in reports] == [
        (FULFILL, "reattested", PREVIOUS), (WAREHOUSE, "reattested", PREVIOUS),
    ]
    assert all(r.current == (V1.fingerprint(), 1) for r in reports)
    after = store.job_state(FULFILL)
    assert after == replace(before, organization_attestation=after.organization_attestation)
    assert after.organization_attestation.definition_fingerprint == V1.fingerprint()

    build(store, V1)[FULFILL].runner.run_trigger(trigger_id="f-2", triggered_at=T0)
    assert store.job_state(FULFILL).run_count == 2
    assert [item.attested for item in observe(store, V1).jobs] == [True, True]
    with pytest.raises(ValueError, match="attestation differs"):
        build(store, V0)[WAREHOUSE].runner.run_trigger(trigger_id="w-1", triggered_at=T0)


def test_running_again_is_a_no_op(opener):
    store = opener()
    started(store)
    reattest(store)
    snapshot = _snapshot(store)
    assert [r.status for r in reattest(store)] == ["already_current", "already_current"]
    assert all(r.scoped_writer_epoch is None for r in reattest(store))
    assert _snapshot(store) == snapshot


@pytest.mark.parametrize("expected, message", [
    (("another-fingerprint", 0), "another definition than expected_previous"),
    ((V0.fingerprint(), 1), "another definition than expected_previous"),
])
def test_compare_and_swap_refuses_without_writing(opener, expected, message):
    store = opener()
    started(store)
    snapshot = _snapshot(store)
    with pytest.raises(ReattestationError, match=message):
        reattest(store, expected=expected)
    assert _snapshot(store) == snapshot


def test_one_job_in_flight_blocks_every_job(opener):
    store = opener()
    started(store, run=False)
    lease = store.claim_scoped_writer(
        WAREHOUSE, "worker-1", expected_epoch=store.scoped_writer_epoch(WAREHOUSE),
    )
    with FencedEnginePersistence(store, lease).transaction() as uow:
        uow.save_job_state(replace(
            uow.get_job_state(WAREHOUSE), status="running", active_trigger_id="w-dead",
        ))
    snapshot = _snapshot(store)
    with pytest.raises(ReattestationError, match="in flight"):
        reattest(store)
    assert _snapshot(store) == snapshot          # FULFILL, checked first, untouched too


def test_a_plain_job_is_refused(opener):
    store = opener()
    plain = SimulationJob(job_id=FULFILL, definition=builtin_catalog().get("warehouse_fulfillment"),
                          persistence=store, backend_factory=_backend)
    PersistentJobRunner(job=plain, owner_id="worker-1").initialize({})
    snapshot = _snapshot(store)
    with pytest.raises(ReattestationError, match="not an attested component job"):
        reattest(store)
    assert _snapshot(store) == snapshot


def test_a_renamed_component_is_refused():
    from sose.persistence.memory import MemoryPersistence

    store = MemoryPersistence()
    started(store, run=False)
    renamed = OrganizationDefinition(
        organization_id=V0.organization_id, version=1,
        components=(
            ProcessComponent("fulfillment-v2", "warehouse_fulfillment", FULFILL,
                             ("FulfillmentOrderChart",)),
            ProcessComponent("warehouse", "warehouse_management", WAREHOUSE,
                             ("WarehouseSiteChart",)),
        ),
        jobs=(ScheduledJob(FULFILL, "fulfillment-v2", "trigger:fulfill"),
              ScheduledJob(WAREHOUSE, "warehouse", "trigger:warehouse")),
    )
    snapshot = _snapshot(store)
    with pytest.raises(ReattestationError, match="component identity changed"):
        reattest(store, renamed)
    assert _snapshot(store) == snapshot


def test_undeclared_and_unregistered_jobs(opener):
    store = opener()
    jobs = build(store, V0)
    jobs[FULFILL].runner.initialize({})            # WAREHOUSE never registered
    reports = {r.job_id: r for r in reattest(store)}
    assert reports[FULFILL].status == "reattested"
    assert reports[WAREHOUSE].status == "absent" and reports[WAREHOUSE].previous is None
    # The absent job registers fresh under the new definition.
    build(store, V1)[WAREHOUSE].runner.initialize({})
    assert [item.attested for item in observe(store, V1).jobs] == [True, True]


def test_a_worker_still_holding_the_old_lease_is_fenced(opener):
    store = opener()
    started(store, run=False)
    old = store.claim_scoped_writer(
        FULFILL, "old-worker", expected_epoch=store.scoped_writer_epoch(FULFILL),
    )
    report = {r.job_id: r for r in reattest(store)}[FULFILL]
    assert report.scoped_writer_epoch == old.epoch + 1
    with pytest.raises(Exception, match="stale"):
        with FencedEnginePersistence(store, old).transaction():
            pass


def test_arguments_are_validated():
    from sose.persistence.memory import MemoryPersistence

    store = MemoryPersistence()
    with pytest.raises(TypeError, match="expected_previous"):
        reattest(store, expected=[V0.fingerprint(), 0])
    with pytest.raises(ValueError, match="worker_id"):
        reattest_component_jobs(
            V1, store, expected_previous=PREVIOUS, catalog=builtin_catalog(),
            manifests=builtin_process_manifests(), consumers=BoundaryConsumerRegistry(),
            worker_id=" ",
        )


def test_a_job_that_changes_after_planning_is_not_overwritten(opener, monkeypatch):
    """The fenced rewrite re-reads the checkpoint; planning saw an older version."""
    store = opener()
    started(store, run=False)
    planned = {job_id: store.job_state(job_id) for job_id in (FULFILL, WAREHOUSE)}
    build(store, V0)[FULFILL].runner.run_trigger(trigger_id="f-1", triggered_at=T0)
    monkeypatch.setattr(store, "job_state", lambda job_id: planned[job_id])
    with pytest.raises(ReattestationError, match="changed while being re-attested"):
        reattest(store)
    monkeypatch.undo()
    state = store.job_state(FULFILL)
    assert state.run_count == 1
    assert state.organization_attestation.definition_fingerprint == V0.fingerprint()
