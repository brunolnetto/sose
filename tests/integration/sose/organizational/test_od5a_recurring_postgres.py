"""Real PostgreSQL scoped-lease falsifiers for bounded #485 source-step bridge.

Same job: two independent PostgreSQL connections must NOT both get an epoch-1
lease. Different jobs must retain independent clocks. Native SimPy resource
arbitration and the boundary reconciler remain subsequent gates.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_recurring import (
    initialize_od5a_recurring_job,
    claim_od5a_recurring_tick, execute_od5a_claimed_tick,
    StaleOD5aRunnerClaimError,
)
from sose.organizational.od5a_durable import od5a_durable_events
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real SOSE_TEST_POSTGRES_DSN required")
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
LEASE = timedelta(seconds=10)


def _namespace():
    return "od5arun_" + uuid4().hex[:16]


def _arm():
    spec = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.5, "execution_slots": 1,
            "coordination_latency": 0.1, "service_cv": 0.5,
        },
        parameter_evidence={
            key: EvidenceClass.ASSUMED for key in
            ("arrival_rate", "execution_slots", "coordination_latency", "service_cv")
        },
    )
    return compile_od5a_arms(
        baseline=spec, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            k: OD5aChargedCost(8, 1, 1, 1)
            for k in ("capacity_only", "topology_only",
                      "coordination_only", "structural_combined")
        },
    )[0]


def _init(namespace):
    with PostgresPersistence(DSN, namespace=namespace) as store:
        initialize_od5a_recurring_job(
            store, job_id="org:race", arm=_arm(),
            items=(OD5aWorkItem(
                "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
            ),),
        )


def _claim_worker(dsn, namespace, owner, barrier, reports):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        # Connections must be opened BEFORE the contested SQL transaction.
        barrier.wait(timeout=30)
        try:
            result = claim_od5a_recurring_tick(
                store, job_id="org:race", owner_id=owner,
                now=NOW, lease_duration=LEASE,
                trigger_id=deterministic_id("od5a-recurring-tick", "org:race", 1),
                _wall_clock=lambda: NOW,
            )
            reports.put((owner, "claimed", result.epoch))
        except StaleOD5aRunnerClaimError:
            reports.put((owner, "fenced", None))


def test_distinct_postgres_connections_cannot_claim_same_step_epoch():
    namespace = _namespace()
    _init(namespace)
    ctx = get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [
        ctx.Process(target=_claim_worker, args=(DSN, namespace, name, barrier, results))
        for name in ("worker:A", "worker:B")
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=45)
            if process.is_alive():
                process.kill()
                process.join()
                pytest.fail("PostgreSQL runner claim hung")
            assert process.exitcode == 0
        outcome = [results.get(timeout=10) for _ in processes]
        assert sorted(state for _, state, _ in outcome) == ["claimed", "fenced"]
        assert sorted(epoch for _, state, epoch in outcome if state == "claimed") == [1]
        with PostgresPersistence(DSN, namespace=namespace) as store:
            state = store.job_state("org:race")
            assert state.next_tick == 0 and state.status == "running"
            with store.transaction() as uow:
                lease = uow.get_entity("od5a_runner_lease", "org:race")
                winner = next(owner for owner, status, _ in outcome if status == "claimed")
                assert lease.attributes["owner_id"] == winner
                assert lease.attributes["epoch"] == 1
            assert not od5a_durable_events(store, job_id="org:race")
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join()
        results.close()
        results.join_thread()


def _kill_after_pg_claim(dsn, namespace):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        claim_od5a_recurring_tick(
            store, job_id="org:race", owner_id="worker:dead",
            now=NOW, lease_duration=LEASE,
            trigger_id=deterministic_id("od5a-recurring-tick", "org:race", 1),
            _wall_clock=lambda: NOW,
        )
        os._exit(91)


def test_recovery_of_postgres_job_after_real_claimed_worker_death():
    namespace = _namespace()
    _init(namespace)
    ctx = get_context("spawn")
    child = ctx.Process(target=_kill_after_pg_claim, args=(DSN, namespace))
    child.start()
    child.join(timeout=40)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("PostgreSQL runner failed to die after claimed step")
    assert child.exitcode == 91
    trigger = deterministic_id("od5a-recurring-tick", "org:race", 1)
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with pytest.raises(StaleOD5aRunnerClaimError):
            claim_od5a_recurring_tick(
                store, job_id="org:race", owner_id="worker:recovered",
                now=NOW + timedelta(seconds=1), lease_duration=LEASE,
                trigger_id=trigger, recover=True,
                _wall_clock=lambda: NOW + timedelta(seconds=1),
            )
        recovered = claim_od5a_recurring_tick(
            store, job_id="org:race", owner_id="worker:recovered",
            now=NOW + timedelta(seconds=11), lease_duration=LEASE,
            trigger_id=trigger, recover=True,
            _wall_clock=lambda: NOW + timedelta(seconds=11),
        )
        assert recovered.epoch == 2
        step = execute_od5a_claimed_tick(
            store, claim=recovered, now=NOW + timedelta(seconds=12),
            _wall_clock=lambda: NOW + timedelta(seconds=12),
        )
        assert step.action == "advanced" and step.logical_tick == 1
        assert len(od5a_durable_events(store, job_id="org:race")) == 1
        assert store.job_state("org:race").last_completed_trigger_id == trigger


def _standalone_source_seed(namespace):
    from sose.organizational.od5a_durable import initialize_od5a_durable
    with PostgresPersistence(DSN, namespace=namespace) as store:
        initialize_od5a_durable(
            store, job_id="org:race", arm=_arm(),
            items=(OD5aWorkItem(
                "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
            ),),
        )


def _ownership_racer(dsn, namespace, kind, barrier, results):
    from sose.organizational.od5a_durable import advance_od5a_durable
    with PostgresPersistence(dsn, namespace=namespace) as store:
        barrier.wait(timeout=30)
        try:
            if kind == "binding":
                initialize_od5a_recurring_job(
                    store, job_id="org:race", arm=_arm(),
                    items=(OD5aWorkItem(
                        "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
                    ),),
                )
            else:
                advance_od5a_durable(store, job_id="org:race")
            results.put((kind, "committed"))
        except (ValueError, RuntimeError) as exc:
            results.put((kind, "rejected"))


def test_postgres_source_advance_cannot_race_recurring_ownership_transfer():
    """Either the standalone step or the job bind commits, never a split pair."""
    namespace = _namespace()
    _standalone_source_seed(namespace)
    ctx = get_context("spawn")
    barrier, reports = ctx.Barrier(2), ctx.Queue()
    procs = [
        ctx.Process(
            target=_ownership_racer,
            args=(DSN, namespace, kind, barrier, reports),
        )
        for kind in ("binding", "advance")
    ]
    try:
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=45)
            if proc.is_alive():
                proc.kill()
                proc.join()
                pytest.fail("PostgreSQL ownership-transfer racer hung")
            assert proc.exitcode == 0
        outcomes = dict(reports.get(timeout=10) for _ in procs)
        assert sorted(outcomes.values()) == ["committed", "rejected"]
        with PostgresPersistence(DSN, namespace=namespace) as store:
            state = store.job_state("org:race")
            with store.transaction() as uow:
                source = uow.get_entity("od5a_durable_pilot", "org:race")
            if outcomes["binding"] == "committed":
                assert state is not None and source.attributes["step"] == state.next_tick == 0
            else:
                assert state is None and source.attributes["step"] == 1
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.kill()
                proc.join()
        reports.close()
        reports.join_thread()
