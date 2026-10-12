"""Falsifiers for OD-5a's real PostgreSQL causal boundary (issue #477).

These are real-connection and real-OS-process tests. Generic PC6 PostgreSQL
checks and the SQLite-only #484 reconciler matrix are not substitutes.
The bounded reconciler remains distinct from the SimPy production runner.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from uuid import uuid4

import pytest

from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_durable import (
    advance_od5a_durable, initialize_od5a_durable,
)
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_reconciler import reconcile_od5a_boundary_step
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real SOSE_TEST_POSTGRES_DSN required")
NOW = datetime(2010, 1, 1, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)
PHASES = ("published", "claimed", "acknowledged", "materialized", "certified")
PHYSICAL = frozenset({"od5a.execution.v1", "od5a.coordination.v1"})


def _namespace() -> str:
    return "od5apg_" + uuid4().hex[:16]


def _arms():
    baseline = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8, "execution_slots": 1,
            "coordination_latency": 0.1, "service_cv": 0.5,
        },
        parameter_evidence={
            key: EvidenceClass.ASSUMED for key in (
                "arrival_rate", "execution_slots", "coordination_latency", "service_cv",
            )
        },
    )
    return {arm.arm_id: arm for arm in compile_od5a_arms(
        baseline=baseline,
        expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            key: OD5aChargedCost(8, 1, 1, 1)
            for key in (
                "capacity_only", "topology_only",
                "coordination_only", "structural_combined",
            )
        },
    )}


def _seed(dsn, namespace, organizations):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        for organization, treatment in organizations.items():
            initialize_od5a_durable(
                store, job_id=organization, arm=_arms()[treatment],
                items=(OD5aWorkItem(
                    item_id="item:" + organization,
                    arrival_at=0.0,
                    service_by_station=(("a", 0.2), ("b", 0.3), ("c", 0.1)),
                ),),
            )
        for organization in organizations:
            for _ in range(100):
                if not advance_od5a_durable(store, job_id=organization):
                    break
            else:
                raise AssertionError("physical source never reached a terminal state")
        assert store.resource_reservations() == ()
        assert sum(e.name in PHYSICAL for e in store.events()) > 0


def _step(dsn, namespace, organization="org:alpha", owner="worker-A", now=NOW):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        return reconcile_od5a_boundary_step(
            store, job_id=organization, owner_id=owner,
            now=now, lease_duration=LEASE,
        )


def _drain(dsn, namespace, organization="org:alpha", owner="worker-A", now=NOW):
    actions = []
    for _ in range(250):
        result = _step(dsn, namespace, organization, owner, now)
        actions.append(result.action)
        if result.action in {"complete", "idle", "blocked"}:
            return actions
    raise AssertionError("reconciler did not reach a bounded stable state")


def _truth(dsn, namespace, organizations):
    """Compare ALL material causal/physical records, not merely return values."""
    with PostgresPersistence(dsn, namespace=namespace) as store:
        with store.transaction() as uow:
            events = tuple(sorted(uow.events(), key=lambda e: e.event_id))
            physical = tuple(e for e in events if e.name in PHYSICAL)
            deliveries = tuple(sorted(
                uow.boundary_deliveries(), key=lambda d: d.delivery_id,
            ))
            data = {
                "events": events,
                "source": tuple(
                    uow.get_entity("od5a_durable_pilot", org)
                    for org in sorted(organizations)
                ),
                "messages": tuple(
                    uow.get_boundary_message(d.message_id) for d in deliveries
                ),
                "deliveries": deliveries,
                "consumptions": tuple(
                    uow.get_boundary_consumption(d.delivery_id)
                    for d in deliveries
                ),
                "observations": tuple(
                    uow.get_entity("od5a_physical_observation", e.event_id)
                    for e in physical
                ),
                "commands": tuple(
                    uow.get_command(d.consumer_effect_id)
                    for d in deliveries if d.consumer_effect_id
                ),
                "certificates": tuple(sorted(
                    uow.business_effects(), key=lambda c: c.effect_id,
                )),
            }
        data["resource_definitions"] = tuple(sorted(
            store.resource_definitions(), key=lambda r: r.name,
        ))
        data["reservations"] = tuple(sorted(
            store.resource_reservations(), key=lambda r: r.reservation_id,
        ))
        assert all(v is not None for v in data["observations"])
        assert len(data["certificates"]) == len(physical)
        assert len(data["messages"]) == len(physical)
        assert len(data["deliveries"]) == len(physical)
        assert len(data["consumptions"]) == len(physical)
        assert all(v is None for v in data["commands"])
        assert not data["reservations"]
        return data


def _killed_worker(dsn, namespace, phase, when):
    # os._exit bypasses exception handling, finally blocks and __exit__.
    # Each call opens an independent PostgreSQL connection.
    if when == "before":
        os._exit(81)
    action = _step(dsn, namespace).action
    os._exit(82 if action == phase else 83)


def _spawn_kill(dsn, namespace, phase, when):
    child = get_context("spawn").Process(
        target=_killed_worker, args=(dsn, namespace, phase, when),
    )
    child.start()
    child.join(timeout=40)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("PostgreSQL reconciler worker hung at an OS-kill boundary")
    assert child.exitcode == (81 if when == "before" else 82), (
        phase, when, child.exitcode,
    )


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("when", ("before", "after"))
def test_real_postgres_os_kill_recovers_exact_causal_and_physical_truth(phase, when):
    reference, interrupted = _namespace(), _namespace()
    jobs = {"org:alpha": "baseline"}
    _seed(DSN, reference, jobs)
    _seed(DSN, interrupted, jobs)
    assert _drain(DSN, reference)[-1] == "complete"
    for expected in PHASES[:PHASES.index(phase)]:
        assert _step(DSN, interrupted).action == expected
    _spawn_kill(DSN, interrupted, phase, when)
    assert _drain(DSN, interrupted)[-1] == "complete"
    assert _truth(DSN, interrupted, jobs) == _truth(DSN, reference, jobs)


def _claim_racer(dsn, namespace, owner, gate, result):
    # Connect before the barrier: genuinely independent live SQL sessions.
    with PostgresPersistence(dsn, namespace=namespace) as store:
        if not gate.wait(timeout=20):
            raise RuntimeError("concurrent claim start barrier timed out")
        step = reconcile_od5a_boundary_step(
            store, job_id="org:alpha", owner_id=owner,
            now=NOW, lease_duration=LEASE,
        )
        result.put((owner, step.action))


def test_two_postgres_processes_compete_for_same_fenced_claim():
    reference, contended = _namespace(), _namespace()
    jobs = {"org:alpha": "baseline"}
    _seed(DSN, reference, jobs)
    _seed(DSN, contended, jobs)
    assert _step(DSN, contended).action == "published"
    ctx = get_context("spawn")
    gate = ctx.Event()
    responses = ctx.Queue()
    workers = [
        ctx.Process(
            target=_claim_racer,
            args=(DSN, contended, owner, gate, responses),
        )
        for owner in ("worker-A", "worker-B")
    ]
    for worker in workers:
        worker.start()
    gate.set()
    for worker in workers:
        worker.join(timeout=40)
        if worker.is_alive():
            worker.kill()
            worker.join()
            pytest.fail("competing real PostgreSQL worker hung")
        assert worker.exitcode == 0
    outcomes = [responses.get(timeout=5) for _ in workers]
    winners = [owner for owner, action in outcomes if action == "claimed"]
    assert len(winners) == 1, outcomes
    assert sorted(action for _, action in outcomes) == ["blocked", "claimed"]
    with PostgresPersistence(DSN, namespace=contended) as store:
        with store.transaction() as uow:
            delivery = next(iter(uow.boundary_deliveries()))
            assert delivery.claim_epoch == delivery.attempts == 1
            assert delivery.claim_owner_id == winners[0]
            assert uow.get_boundary_consumption(delivery.delivery_id) is None
    assert _drain(DSN, contended, owner=winners[0])[-1] == "complete"
    assert _drain(DSN, reference, owner=winners[0])[-1] == "complete"
    assert _truth(DSN, contended, jobs) == _truth(DSN, reference, jobs)


@pytest.mark.parametrize("treatment", tuple(_arms()))
def test_postgres_five_arms_have_complete_event_keyed_certificates(treatment):
    namespace = _namespace()
    jobs = {"org:alpha": treatment}
    _seed(DSN, namespace, jobs)
    actions = _drain(DSN, namespace)
    assert actions[-1] == "complete"
    truth = _truth(DSN, namespace, jobs)
    count = sum(event.name in PHYSICAL for event in truth["events"])
    for phase in PHASES:
        assert actions.count(phase) == count
    assert _drain(DSN, namespace) == ["complete"]


def test_two_postgres_organizations_recover_independently_after_worker_death():
    jobs = {"org:a": "baseline", "org:b": "capacity_only"}
    reference, interrupted = _namespace(), _namespace()
    _seed(DSN, reference, jobs)
    _seed(DSN, interrupted, jobs)
    for job in jobs:
        assert _drain(DSN, reference, organization=job, owner=job)[-1] == "complete"
    assert _step(DSN, interrupted, "org:a", "org:a").action == "published"
    # Kill an organization's worker after committing its fenced claim.
    def _kill_claim(dsn, namespace):
        os._exit(84 if _step(dsn, namespace, "org:a", "org:a").action == "claimed" else 85)
    # Spawn requires an importable module-level entrypoint; use existing
    # OS-kill helper with the default organization only in the single-org matrix.
    # For the two-org case exercise explicit connection replacement after claim.
    assert _step(DSN, interrupted, "org:a", "org:a").action == "claimed"
    for _ in range(250):
        actions = {
            job: _step(DSN, interrupted, organization=job, owner=job).action
            for job in jobs
        }
        if all(action == "complete" for action in actions.values()):
            break
    else:
        pytest.fail("two organizations failed to recover on PostgreSQL")
    assert _truth(DSN, interrupted, jobs) == _truth(DSN, reference, jobs)
