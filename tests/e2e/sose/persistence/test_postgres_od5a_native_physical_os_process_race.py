"""#477: two independent PostgreSQL OS workers cannot grant one physical unit twice.

This is a bounded native-grant arbitration falsifier, NOT five-arm scientific
equivalence, server/network failure certification, or full durable fairness.
"""
from __future__ import annotations

import os
from copy import deepcopy
from multiprocessing import get_context
from uuid import uuid4

import pytest

from sose.organizational import od5a_recurring
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_od5a_shared_native_physical_authority import (
    _binding, _seed, _source, _tick,
)

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="live SOSE_TEST_POSTGRES_DSN required")


def _separate_owner(dsn, namespace, org, barrier, answers):
    """A fresh Python interpreter and fresh PG connection for each owner."""
    original = od5a_recurring._RecurringNativePhysicalArbiter.prepare

    def meet_at_native_grant(self, *, releases, grants, at):
        original(self, releases=releases, grants=grants, at=at)
        if grants and self.execution_pool == f"{org}/execution-pool":
            barrier.wait(timeout=30)

    od5a_recurring._RecurringNativePhysicalArbiter.prepare = meet_at_native_grant
    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            action = _tick(store, org, _binding("unit-1")).action
            answers.put((org, action))
    except BaseException as exc:
        answers.put((org, f"ERROR: {type(exc).__name__}: {exc}"))
        raise


def test_pg_independent_os_processes_cannot_commit_same_native_unit_twice():
    """Both workers observe a native SimPy grant before either DB arbiter commits.

    Only the shared PostgreSQL physical authority may decide the winner; a
    second speculative native grant must downgrade to durable waiting with no
    committed source effect. No cross-owner process-local lock is sufficient.
    """
    assert DSN is not None
    namespace = "od5a_os_physical_" + uuid4().hex[:12]
    owners = ("org:os-one", "org:os-two")
    binding = _binding("unit-1")
    with PostgresPersistence(DSN, namespace=namespace) as seed:
        for org in owners:
            _seed(seed, org, binding)

    ctx = get_context("spawn")
    barrier = ctx.Barrier(2)
    answers = ctx.Queue()
    workers = [
        ctx.Process(
            target=_separate_owner,
            args=(DSN, namespace, org, barrier, answers),
            name=f"od5a-{org}",
        )
        for org in owners
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=65)
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        assert all(worker.exitcode == 0 for worker in workers), [
            (worker.name, worker.exitcode) for worker in workers
        ]
        results = dict(answers.get(timeout=10) for _ in owners)
        assert set(results) == set(owners)
        assert sorted(results.values()) == ["advanced", "waiting"], results
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
            worker.close()
        answers.close()
        answers.join_thread()

    winner = next(org for org, action in results.items() if action == "advanced")
    loser = next(org for org, action in results.items() if action == "waiting")
    with PostgresPersistence(DSN, namespace=namespace) as reopened:
        physical = reopened.temporal_resources()
        bookings = physical.reservations()
        assert len(bookings) == 1
        assert bookings[0].owner_id == winner
        assert bookings[0].address.instance_id == "unit-1"
        assert _source(reopened, winner)["step"] == 1
        assert _source(reopened, loser)["step"] == 0
        assert not any(event.owner_id == loser for event in reopened.events())
        assert reopened.job_state(loser).run_count == 0
        assert physical.audit()



def _multi_instance_owner(dsn, namespace, org, instance, barrier, answers):
    """Owner-scoped native preflight + independent real PostgreSQL writer."""
    original_prepare = od5a_recurring._RecurringNativePhysicalArbiter.prepare

    def meet_at_native_grant(self, *, releases, grants, at):
        original_prepare(self, releases=releases, grants=grants, at=at)
        if grants and self.execution_pool == f"{org}/execution-pool":
            assert len(grants) == 1
            # All THREE fresh interpreters witness local native permits before
            # the shared pool's SQL arbitration can resolve either instance.
            barrier.wait(timeout=40)

    od5a_recurring._RecurringNativePhysicalArbiter.prepare = meet_at_native_grant
    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            action = _tick(store, org, _binding(instance)).action
            answers.put((org, instance, action))
    except BaseException as exc:
        answers.put((org, instance, f"ERROR: {type(exc).__name__}: {exc}"))
        raise


def test_pg_three_os_workers_two_physical_instances_conserve_shared_capacity():
    """Three real PG workers compete for pool capacity=2; exactly two succeed.

    Two independent owners choose the same instance; the third chooses the
    other instance. One loser must preserve source and causal work for retry.
    The test validates the shared physical instance ledger, not FIFO fairness,
    preemption, a whole-server crash, or complete cross-domain ACK effects.
    """
    assert DSN is not None
    namespace = "od5a_os_pool2_" + uuid4().hex[:12]
    owners = (
        ("org:pool2-a", "unit-1"),
        ("org:pool2-b", "unit-2"),
        ("org:pool2-c", "unit-1"),
    )
    seeded_sources = {}
    with PostgresPersistence(DSN, namespace=namespace) as seed:
        for org, instance in owners:
            _seed(seed, org, _binding(instance))
            seeded_sources[org] = deepcopy(_source(seed, org))

    ctx = get_context("spawn")
    barrier = ctx.Barrier(3)
    answers = ctx.Queue()
    workers = [
        ctx.Process(
            target=_multi_instance_owner,
            args=(DSN, namespace, org, instance, barrier, answers),
            name=f"od5a-{org}",
        )
        for org, instance in owners
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=85)
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        assert all(worker.exitcode == 0 for worker in workers), [
            (worker.name, worker.exitcode) for worker in workers
        ]
        outcomes = {org: (instance, action)
                    for org, instance, action in
                    (answers.get(timeout=10) for _ in owners)}
        assert set(outcomes) == {org for org, _ in owners}
        assert sorted(action for _, action in outcomes.values()) == [
            "advanced", "advanced", "waiting",
        ], outcomes
        assert outcomes["org:pool2-b"] == ("unit-2", "advanced")
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
            worker.close()
        answers.close()
        answers.join_thread()

    winners = {org for org, (_, action) in outcomes.items()
               if action == "advanced"}
    loser = next(org for org, (_, action) in outcomes.items()
                 if action == "waiting")
    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        ledger = recovered.temporal_resources()
        reservations = ledger.reservations()
        assert len(reservations) == 2
        assert {item.owner_id for item in reservations} == winners
        assert {(item.owner_id, item.address.instance_id)
                for item in reservations} == {
            (org, instance) for org, instance in owners if org in winners
        }
        assert all(item.status == "reserved" for item in reservations)
        for org, instance in owners:
            action = outcomes[org][1]
            source = _source(recovered, org)
            job = recovered.job_state(org)
            if action == "advanced":
                assert source["step"] == 1
                assert job.run_count == 1
                assert any(e.owner_id == org for e in recovered.events())
            else:
                assert org == loser and source["step"] == 0
                assert source == seeded_sources[org], (
                    "rejected writer mutated pending source or logical clock"
                )
                assert job.run_count == 0
                assert job.active_trigger_id is not None
                assert not any(e.owner_id == org for e in recovered.events())
                assert not any(
                    item.resource_name.startswith(org + "/")
                    for item in recovered.resource_reservations()
                )
                # A rejected native grant is a durable, causal pending wait.
                from sose.core.identity import deterministic_id
                wait_id = deterministic_id(
                    "od5a-shared-capacity-wait", org, job.active_trigger_id,
                )
                wait = recovered.entity("od5a_shared_capacity_wait", wait_id)
                assert wait is not None and wait.state == "waiting"
                assert wait.attributes["requests"][0]["instance_id"] == instance
        assert ledger.audit()

    # Independently reopened namespace must retain exact stable owners and
    # no new booking on a read-only recovery.
    with PostgresPersistence(DSN, namespace=namespace) as reopened:
        assert reopened.temporal_resources().reservations() == reservations
        assert reopened.temporal_resources().audit()
