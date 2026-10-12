"""Falsifiers for OD-5a's real PostgreSQL causal boundary (issue #477).

These are real-connection and real-OS-process tests. Generic PC6 PostgreSQL
checks and the SQLite-only #484 reconciler matrix are not substitutes.
The bounded reconciler remains distinct from the SimPy production runner.
"""
from __future__ import annotations

import os
import queue
from contextlib import contextmanager
from time import monotonic, sleep
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
from sose.organizational.od5a_boundary import consume_od5a_physical_completion
from sose.composition.boundary import BoundaryLease, StaleBoundaryClaimError
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
            # Enumerate complete UoW collections, not only IDs reachable
            # from delivered messages. An orphan must fail this oracle.
            records = uow._working
            messages = tuple(sorted(records.boundary_messages.values(), key=lambda m: m.message_id))
            consumptions = tuple(sorted(records.boundary_consumptions.values(), key=lambda c: c.delivery_id))
            observations = tuple(sorted((e for e in records.entities.values()
                if e.entity_type == "od5a_physical_observation"), key=lambda e: e.id))
            commands = tuple(sorted(records.commands.values(), key=lambda c: c.command_id))
            data = {
                "events": events,
                "source": tuple(
                    uow.get_entity("od5a_durable_pilot", org)
                    for org in sorted(organizations)
                ),
                "messages": messages,
                "deliveries": deliveries,
                "consumptions": consumptions,
                "observations": observations,
                "commands": commands,
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
        assert {e.id for e in data["observations"]} == {e.event_id for e in physical}
        assert {m.message_id for m in data["messages"]} == {d.message_id for d in data["deliveries"]}
        assert {c.delivery_id for c in data["consumptions"]} == {d.delivery_id for d in data["deliveries"]}
        assert len(data["certificates"]) == len(physical)
        assert len(data["messages"]) == len(physical)
        assert len(data["deliveries"]) == len(physical)
        assert len(data["consumptions"]) == len(physical)
        assert not data["commands"]
        assert not data["reservations"]
        return data


def _killed_worker(dsn, namespace, phase, when):
    # os._exit bypasses exception handling, finally blocks and __exit__.
    # Each call opens an independent PostgreSQL connection.
    if when == "before":
        os._exit(81)
    # Keep the SQL connection open: the OS, not __exit__, tears it down.
    # The inner reconciler UoW has committed before it returns.
    with PostgresPersistence(dsn, namespace=namespace) as store:
        action = reconcile_od5a_boundary_step(
            store, job_id="org:alpha", owner_id="worker-A",
            now=NOW, lease_duration=LEASE,
        ).action
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


def _claim_racer(dsn, namespace, owner, ready, start, signals, release, result):
    """Instrument the actual boundary UoW, not just worker scheduling."""
    with PostgresPersistence(dsn, namespace=namespace) as store:
        # PostgresPersistence bootstrap serializes schema initialization using
        # a namespace advisory lock. Both workers must establish connections
        # BEFORE the contested boundary transaction is deliberately held.
        ready.put(owner)
        if not start.wait(timeout=30):
            raise RuntimeError("racer start barrier timed out")
        backend_pid = store._connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        original = store.boundary_transaction

        @contextmanager
        def intercepted(**kwargs):
            if owner == "worker-B":
                signals.put(("attempting_sql_lock", owner, backend_pid))
            with original(**kwargs) as uow:
                if owner == "worker-A":
                    # The exclusive PostgreSQL advisory transaction lock is held.
                    signals.put(("holding_sql_lock", owner, backend_pid))
                    if not release.wait(timeout=30):
                        raise RuntimeError("claim lock release gate timed out")
                yield uow

        store.boundary_transaction = intercepted
        step = reconcile_od5a_boundary_step(
            store, job_id="org:alpha", owner_id=owner,
            now=NOW, lease_duration=LEASE,
        )
        result.put((owner, step.action))


def test_two_postgres_processes_compete_for_same_fenced_claim():
    import psycopg

    reference, contended = _namespace(), _namespace()
    jobs = {"org:alpha": "baseline"}
    _seed(DSN, reference, jobs)
    _seed(DSN, contended, jobs)
    assert _step(DSN, contended).action == "published"
    ctx = get_context("spawn")
    release = ctx.Event()
    start_a, start_b = ctx.Event(), ctx.Event()
    ready, signals, responses = ctx.Queue(), ctx.Queue(), ctx.Queue()
    a = ctx.Process(
        target=_claim_racer,
        args=(DSN, contended, "worker-A", ready, start_a, signals, release, responses),
    )
    b = ctx.Process(
        target=_claim_racer,
        args=(DSN, contended, "worker-B", ready, start_b, signals, release, responses),
    )
    a.start()
    b.start()
    try:
        assert {ready.get(timeout=30) for _ in (a, b)} == {"worker-A", "worker-B"}
        start_a.set()
        holding = signals.get(timeout=30)
        assert holding[:2] == ("holding_sql_lock", "worker-A")
        a_pid = holding[2]
        start_b.set()
        attempting = signals.get(timeout=30)
        assert attempting[:2] == ("attempting_sql_lock", "worker-B")
        b_pid = attempting[2]
        assert a_pid != b_pid
        # Establish actual server-side contention: a distinct backend must
        # be waiting on a NOT-GRANTED advisory lock, not merely queued by
        # Python's scheduler after announcing an attempted call.
        with psycopg.connect(DSN, autocommit=True) as observer:
            lock_key = contended + ":causal-boundary"
            assert observer.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                (lock_key,),
            ).fetchone() == (False,), "A is not holding the PostgreSQL boundary lock"
            deadline = monotonic() + 8
            waiting = False
            while monotonic() < deadline:
                row = observer.execute(
                    """
                    SELECT count(*)
                    FROM pg_locks AS waiting
                    JOIN pg_locks AS held
                      ON waiting.database IS NOT DISTINCT FROM held.database
                     AND waiting.classid = held.classid
                     AND waiting.objid = held.objid
                     AND waiting.objsubid = held.objsubid
                    WHERE waiting.pid = %s AND held.pid = %s
                      AND waiting.locktype = 'advisory'
                      AND held.locktype = 'advisory'
                      AND waiting.granted = false
                      AND held.granted = true
                    """,
                    (b_pid, a_pid),
                ).fetchone()
                if row and row[0] >= 1:
                    waiting = True
                    break
                sleep(0.05)
            assert waiting, "worker B never waited for the specific advisory lock held by worker A"
        with pytest.raises(queue.Empty):
            responses.get(timeout=0.5)
    finally:
        release.set()
        for worker in (a, b):
            if worker.pid is None:
                continue
            worker.join(timeout=40)
            if worker.is_alive():
                worker.kill()
                worker.join()
    assert a.exitcode == b.exitcode == 0
    outcomes = [responses.get(timeout=5) for _ in (a, b)]
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


def _killed_organization_worker(dsn, namespace, organization, owner):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        action = reconcile_od5a_boundary_step(
            store, job_id=organization, owner_id=owner,
            now=NOW, lease_duration=LEASE,
        ).action
        os._exit(84 if action == "claimed" else 85)


def test_two_postgres_organizations_recover_independently_after_worker_death():
    jobs = {"org:a": "baseline", "org:b": "capacity_only"}
    reference, interrupted = _namespace(), _namespace()
    _seed(DSN, reference, jobs)
    _seed(DSN, interrupted, jobs)
    for job in jobs:
        assert _drain(DSN, reference, organization=job, owner=job)[-1] == "complete"
    assert _step(DSN, interrupted, "org:a", "org:a").action == "published"
    # A real OS-killed worker keeps its committed claim; another process
    # reopens the same PostgreSQL namespace and finishes both organizations.
    child = get_context("spawn").Process(
        target=_killed_organization_worker,
        args=(DSN, interrupted, "org:a", "org:a"),
    )
    child.start()
    child.join(timeout=40)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("organization worker deadlocked")
    assert child.exitcode == 84
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


def test_killed_postgres_claim_requires_new_epoch_and_rejects_stale_owner():
    """An orphan may never ACK as its dead worker merely because the lease lives."""
    jobs = {"org:alpha": "baseline"}
    reference, interrupted = _namespace(), _namespace()
    _seed(DSN, reference, jobs)
    _seed(DSN, interrupted, jobs)
    assert _step(DSN, reference).action == "published"
    assert _step(DSN, interrupted).action == "published"
    assert _step(DSN, reference).action == "claimed"
    child = get_context("spawn").Process(
        target=_killed_organization_worker,
        args=(DSN, interrupted, "org:alpha", "worker-A"),
    )
    child.start()
    child.join(timeout=40)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("claiming PostgreSQL worker deadlocked")
    assert child.exitcode == 84
    with PostgresPersistence(DSN, namespace=interrupted) as store:
        with store.transaction() as uow:
            d = next(iter(uow.boundary_deliveries()))
            stale = BoundaryLease(
                delivery_id=d.delivery_id, message_id=d.message_id,
                owner_id=d.claim_owner_id, epoch=d.claim_epoch,
                lease_expires_at=d.lease_expires_at,
            )
            assert stale.epoch == 1
    now = NOW + LEASE
    assert _step(DSN, reference, owner="worker-B", now=now).action == "claimed"
    assert _step(DSN, interrupted, owner="worker-B", now=now).action == "claimed"
    with PostgresPersistence(DSN, namespace=interrupted) as store:
        with store.transaction() as uow:
            d = next(iter(uow.boundary_deliveries()))
            assert d.claim_epoch == d.attempts == 2
            assert d.claim_owner_id == "worker-B"
        with pytest.raises(StaleBoundaryClaimError):
            consume_od5a_physical_completion(store, lease=stale, now=now)
    assert _drain(DSN, reference, owner="worker-B", now=now)[-1] == "complete"
    assert _drain(DSN, interrupted, owner="worker-B", now=now)[-1] == "complete"
    assert _truth(DSN, interrupted, jobs) == _truth(DSN, reference, jobs)
