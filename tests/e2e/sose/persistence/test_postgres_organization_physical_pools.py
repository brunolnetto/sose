"""#494/#496: physical pools admitted against the PostgreSQL resource ledger.

A ``PhysicalPoolRef`` is only names, so admission used to refuse every one. With
``snapshot_organization(..., physical_ledger=True)`` it is admitted when the ledger
confirms it (``sose.composition.physical_pools``), and the snapshot lists the pool
and this organization's reservations by component, inside the digest.
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.composition.organization import PhysicalPoolRef
from sose.composition.organization_snapshot import snapshot_organization
from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import TemporalReservation
from sose.persistence.memory import MemoryPersistence
from sose.persistence.postgres import PostgresPersistence
from tests.support import organization_workers as org

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
T0 = datetime(2026, 11, 26, 9, tzinfo=timezone.utc)
H = timedelta(hours=1)
ORGANIZATION = org.definition().organization_id
FULFILL, WAREHOUSE = org.FULFILL, org.WAREHOUSE        # component owner ids


def _pool(*, scope="organization", organization=ORGANIZATION, pool_id="dock-a"):
    return ResourcePoolContract(
        resource_type="dock", pool_id=pool_id, scope=scope,
        organization_id=organization if scope == "organization" else None,
        capacity=2, instance_ids=("dock-1", "dock-2"),
    )


def _reserve(ledger, pool, rid, owner, instance, start=0):
    ledger.reserve(TemporalReservation(
        reservation_id=rid, address=pool.address(instance_id=instance), owner_id=owner,
        start_at=T0 + start * H, end_at=T0 + (start + 2) * H, causation_id=f"cause:{rid}",
    ))


def _definition(*, owner=ORGANIZATION, consumers=("fulfillment",), pool_id="dock-a"):
    return replace(org.definition(), physical_pools=(
        PhysicalPoolRef(pool_id=pool_id, owner_id=owner, consumer_ids=consumers),))


def _store(pool=None):
    store = PostgresPersistence(DSN, namespace="pools_" + uuid4().hex[:12])
    ledger = store.temporal_resources()
    if pool is not None:
        ledger.register_pool(pool)
    return store, ledger


def _snapshot(definition, store, **kwargs):
    return snapshot_organization(definition, store, **org.admission(), **kwargs)


def test_without_the_ledger_a_pool_is_still_refused():
    store, _ledger = _store(_pool())
    with store, pytest.raises(ValueError, match="require authoritative resource admission"):
        _snapshot(_definition(), store)


def test_an_admitted_pool_lists_its_reservations_by_component():
    pool = _pool()
    store, ledger = _store(pool)
    with store:
        _reserve(ledger, pool, "r-1", FULFILL, "dock-1")
        first = _snapshot(_definition(), store, physical_ledger=True)
        (snapshot,) = first.physical_pools
        assert (snapshot.pool_id, snapshot.scope, snapshot.capacity) == ("dock-a", "organization", 2)
        assert [(r.component_id, r.reservation_id, r.instance_id, r.status)
                for r in snapshot.reservations] == [("fulfillment", "r-1", "dock-1", "reserved")]
        assert snapshot.external_reserved_units == 0
        assert _snapshot(_definition(), store, physical_ledger=True).digest == first.digest
        _reserve(ledger, pool, "r-2", FULFILL, "dock-2")
        assert _snapshot(_definition(), store, physical_ledger=True).digest != first.digest


def test_a_snapshot_without_pools_keeps_its_digest():
    store, _ledger = _store()
    with store:
        plain = _snapshot(org.definition(), store)
        assert plain.physical_pools == ()
        assert snapshot_organization(org.definition(), MemoryPersistence(),
                                     **org.admission()).digest == plain.digest


@pytest.mark.parametrize("pool, definition, message", [
    (None, _definition(), "not registered in the resource ledger"),
    (_pool(pool_id="other"), _definition(), "not registered in the resource ledger"),
    (_pool(organization="org:someone-else"), _definition(owner="org:someone-else"),
     "belongs to organization org:someone-else"),
    # owner_id names the scope: a wrong owner or scope finds no pool there.
    (_pool(), _definition(owner="resource-ledger"), "not registered .* in scope resource-ledger"),
    (_pool(scope="shared"), _definition(), "not registered .* in scope org:equivalence"),
])
def test_a_pool_the_ledger_does_not_confirm_is_refused(pool, definition, message):
    store, _ledger = _store(pool)
    with store, pytest.raises(ValueError, match=message):
        _snapshot(definition, store, physical_ledger=True)


def test_a_component_that_is_not_a_declared_consumer_cannot_hold_a_reservation():
    pool = _pool()
    store, ledger = _store(pool)
    with store:
        _reserve(ledger, pool, "r-1", WAREHOUSE, "dock-1")
        with pytest.raises(ValueError, match="without being a declared consumer"):
            _snapshot(_definition(consumers=("fulfillment",)), store, physical_ledger=True)


def test_an_organization_pool_refuses_a_foreign_owner():
    pool = _pool()
    store, ledger = _store(pool)
    with store:
        _reserve(ledger, pool, "r-1", "org:intruder:owner", "dock-1")
        with pytest.raises(ValueError, match="reservation by foreign owner"):
            _snapshot(_definition(), store, physical_ledger=True)


def test_a_shared_pool_counts_other_owners_without_listing_them():
    pool = _pool(scope="shared")
    store, ledger = _store(pool)
    with store:
        _reserve(ledger, pool, "r-own", FULFILL, "dock-1")
        _reserve(ledger, pool, "r-other", "org:neighbour:owner", "dock-2")
        (snapshot,) = _snapshot(_definition(owner="shared"), store,
                                physical_ledger=True).physical_pools
        assert [r.reservation_id for r in snapshot.reservations] == ["r-own"]
        assert snapshot.external_reserved_units == 1


def test_the_ledger_needs_postgresql():
    with pytest.raises(TypeError, match="PostgreSQL resource ledger"):
        snapshot_organization(_definition(), MemoryPersistence(), **org.admission(),
                              physical_ledger=True)


def test_another_tenants_pool_with_the_same_id_does_not_interfere():
    ours, theirs = _pool(), _pool(organization="org:other-tenant")
    store, ledger = _store(ours)
    with store:
        ledger.register_pool(theirs)
        _reserve(ledger, ours, "r-1", FULFILL, "dock-1")
        (snapshot,) = _snapshot(_definition(), store, physical_ledger=True).physical_pools
        assert snapshot.organization_id == ORGANIZATION
        assert [r.reservation_id for r in snapshot.reservations] == ["r-1"]


def test_a_released_reservation_is_history_not_a_violation():
    pool = _pool()
    store, ledger = _store(pool)
    with store:
        _reserve(ledger, pool, "r-old", WAREHOUSE, "dock-1")       # a non-consumer
        ledger.release("r-old", at=T0 + H)
        (snapshot,) = _snapshot(_definition(), store, physical_ledger=True).physical_pools
        assert [(r.component_id, r.status) for r in snapshot.reservations] == [
            ("warehouse", "released")]


def test_admission_refuses_pools_when_components_share_an_owner():
    from sose.composition.organization_admission import validate_organization_admission
    from sose.composition.physical_pools import read_physical_ledger

    pool = _pool()
    store, _ledger = _store(pool)
    shared = replace(_definition(), components=tuple(
        replace(component, owner_id=FULFILL) for component in org.definition().components))
    with store, pytest.raises(ValueError, match="distinct owner_id"):
        validate_organization_admission(shared, **org.admission(),
                                        physical_ledger=read_physical_ledger(store))


def test_without_pools_the_ledger_is_not_read():
    store = PostgresPersistence(DSN, namespace="pools_" + uuid4().hex[:12])   # no ledger tables
    with store:
        assert _snapshot(org.definition(), store, physical_ledger=True).physical_pools == ()


def test_a_pool_snapshot_refuses_an_outer_read_snapshot():
    pool = _pool()
    store, _ledger = _store(pool)
    with store, store.read_snapshot():
        with pytest.raises(RuntimeError, match="outer read snapshot"):
            _snapshot(_definition(), store, physical_ledger=True)


def _run_jobs(definition, store, **kwargs):
    from sose.composition.organization_jobs import build_attested_component_jobs

    jobs = build_attested_component_jobs(definition, store, backend_factory=org._backend,
                                         worker_id="w", **org.admission(), **kwargs)
    for job in jobs:
        job.runner.initialize({})
        for index in range(org.TRIGGERS):
            job.runner.run_trigger(trigger_id=org.trigger_id(job.scheduled.job_id, index),
                                   triggered_at=org.T0)
    return jobs


def test_jobs_of_an_organization_with_pools_build_and_run_with_the_ledger():
    from sose.composition.boundary_observation import inspect_selected_boundary_protocol
    from sose.composition.observation import observe_organization_events
    from sose.composition.organization_reattest import reattest_component_jobs

    pool = _pool()
    store, ledger = _store(pool)
    definition = _definition()
    with store:
        _reserve(ledger, pool, "r-1", FULFILL, "dock-1")
        with pytest.raises(ValueError, match="require authoritative resource admission"):
            _run_jobs(definition, store)
        jobs = _run_jobs(definition, store, physical_ledger=True)
        assert jobs
        snapshot = _snapshot(definition, store, physical_ledger=True)
        assert [c.machines for c in snapshot.components if c.machines]
        assert [r.reservation_id for r in snapshot.physical_pools[0].reservations] == ["r-1"]
        evidence = observe_organization_events(definition, store, **org.admission(),
                                               physical_ledger=True)
        assert evidence.definition_fingerprint == snapshot.definition_fingerprint
        assert any(component.event_ids for component in evidence.components)
        inspected = inspect_selected_boundary_protocol(
            definition, store, message_ids=(), **org.admission(), physical_ledger=True)
        assert inspected.definition_fingerprint == snapshot.definition_fingerprint
        reports = reattest_component_jobs(
            definition, store, expected_previous=(snapshot.definition_fingerprint,
                                                  definition.config_epoch),
            **org.admission(), worker_id="w", physical_ledger=True,
        )
        assert {report.status for report in reports} == {"already_current"}


def test_a_job_build_refuses_a_pool_the_ledger_contradicts():
    pool = _pool()
    store, ledger = _store(pool)
    with store:
        _reserve(ledger, pool, "r-1", WAREHOUSE, "dock-1")     # not a declared consumer
        with pytest.raises(ValueError, match="without being a declared consumer"):
            _run_jobs(_definition(), store, physical_ledger=True)


def test_the_organization_consumer_registry_admits_pools_from_a_ledger_view():
    from sose.composition.organization_runtime import (
        OrganizationBoundaryConsumerRegistry as OrganizationRuntime,
    )
    from sose.composition.physical_pools import read_physical_ledger

    pool = _pool()
    store, _ledger = _store(pool)
    admission = org.admission()
    arguments = dict(catalog=admission["catalog"], manifests=admission["manifests"],
                     contracts=admission["consumers"])
    with store:
        with pytest.raises(ValueError, match="require authoritative resource admission"):
            OrganizationRuntime(_definition(), **arguments)
        runtime = OrganizationRuntime(_definition(), **arguments,
                                      physical_ledger_view=read_physical_ledger(store))
        assert runtime.definition_fingerprint


def test_job_admission_without_pools_reads_no_ledger_and_needs_postgresql_with_pools():
    from sose.composition.physical_pools import ledger_for

    memory = MemoryPersistence()
    assert ledger_for(memory, True, org.definition()) is None      # no pools: no read
    with pytest.raises(TypeError, match="PostgreSQL resource ledger"):
        ledger_for(memory, True, _definition())
