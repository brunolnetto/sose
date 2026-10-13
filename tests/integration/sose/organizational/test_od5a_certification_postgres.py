"""OD-5a #490: PostgreSQL independent processes, scope overlap and causal oracle.

Proves the bounded adapter's real server-side concurrency, not just two
SQLite workers on different local clocks. The oracle is the standalone #488
reducer and reconciler, in a separately initialized PostgreSQL namespace.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from multiprocessing import get_context
import os
from uuid import uuid4

import pytest

from sose.composition.boundary import BoundaryService
from sose.composition.model import BoundaryMessage, DeliveryStatus
from sose.organizational.od5a_durable import initialize_od5a_durable
from sose.organizational.od5a_recurring import (
    initialize_od5a_recurring_job, run_od5a_recurring_tick,
)
from sose.organizational.od5a_reference_runner import advance_od5a_reference_occurrence
from sose.persistence.postgres import PostgresPersistence

from tests.integration.sose.organizational.test_od5a_certification_crash import (
    LEASE, NOW, _arm, _certificate, _integrated, _items, _next_trigger,
)

DSN = os.getenv("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="live PostgreSQL DSN required")

ORGS = ("org:manufacturing-like", "org:order-like")


def _pg_namespace(prefix):
    return prefix + uuid4().hex[:12]


def _process_loop(dsn, namespace, org, requests, answers):
    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            while (request := requests.get()) is not None:
                owner, now = request
                result = _integrated(store, org, owner, now)
                answers.put((result.action, None))
    except BaseException as exc:
        import traceback
        answers.put(("error", f"{exc!r}\n{traceback.format_exc()}"))


def _scoped_publish_process(dsn, namespace, org, barrier, answers):
    """Make two *real* PostgreSQL boundary critical sections overlap."""
    from sose.organizational.od5a_integrated import advance_od5a_integrated_occurrence
    original = PostgresPersistence.boundary_transaction
    entered = False

    @contextmanager
    def instrument(self, *, owner_epoch=None, writer_scope=None):
        nonlocal entered
        if owner_epoch is None or writer_scope != "od5a:boundary:" + org:
            raise AssertionError("boundary failed to use organization-scoped writer fencing")
        with original(
            self, owner_epoch=owner_epoch, writer_scope=writer_scope,
        ) as uow:
            if not entered:
                entered = True
                # If either actor holds the namespace-exclusive lock this
                # barrier times out; true organization-scoped locks coexist.
                barrier.wait(timeout=25)
            yield uow

    try:
        PostgresPersistence.boundary_transaction = instrument
        with PostgresPersistence(dsn, namespace=namespace) as store:
            result = advance_od5a_integrated_occurrence(
                store, job_id=org, owner_id="publisher:" + org,
                now=NOW, lease_duration=LEASE,
                source_trigger_id=_next_trigger(store, org),
            )
            answers.put((org, result.action, entered))
    except BaseException as exc:
        import traceback
        answers.put((org, "ERROR:" + repr(exc) + "\n" + traceback.format_exc(), entered))


def _join_all(processes, *, timeout=45):
    for process in processes:
        process.join(timeout=timeout)
    try:
        assert all(not p.is_alive() for p in processes), "PostgreSQL workers hung"
        assert all(p.exitcode == 0 for p in processes), [
            p.exitcode for p in processes
        ]
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join()


def test_two_real_postgres_processes_overlap_scoped_boundary_transactions():
    namespace = _pg_namespace("od5a_overlap_")
    with PostgresPersistence(DSN, namespace=namespace) as seed:
        for org in ORGS:
            initialize_od5a_recurring_job(
                seed, job_id=org, arm=_arm(), items=_items(org),
            )
        # Persist each organization's first physical event without publishing
        # it yet. The *next* integrated call must enter the boundary section.
        for org in ORGS:
            for _ in range(30):
                result = run_od5a_recurring_tick(
                    seed, job_id=org, owner_id="source:" + org,
                    now=NOW, lease_duration=LEASE,
                    trigger_id=_next_trigger(seed, org),
                )
                assert result.action == "advanced"
                with seed.transaction() as uow:
                    events = tuple(
                        e for e in uow.events()
                        if e.entity_id == org and
                        e.name in {"od5a.execution.v1", "od5a.coordination.v1"}
                    )
                if events:
                    break
            else:
                pytest.fail("source did not generate an authoritative physical event")

    ctx = get_context("spawn")
    barrier, answers = ctx.Barrier(2), ctx.Queue()
    processes = [
        ctx.Process(
            target=_scoped_publish_process,
            args=(DSN, namespace, org, barrier, answers),
        ) for org in ORGS
    ]
    try:
        for process in processes:
            process.start()
        _join_all(processes)
        outcomes = [answers.get(timeout=10) for _ in processes]
        assert sorted(outcomes) == [(org, "published", True) for org in ORGS]
        with PostgresPersistence(DSN, namespace=namespace) as store:
            with store.transaction() as uow:
                messages = tuple(
                    uow.get_boundary_message(d.message_id)
                    for d in uow.boundary_deliveries()
                )
            assert {m.correlation_id for m in messages} == set(ORGS)
            assert len(messages) == 2
    finally:
        answers.close()
        answers.join_thread()


def test_two_organizations_recover_on_separate_postgres_processes_equal_oracle():
    ref_ns, run_ns = _pg_namespace("od5a_ref_"), _pg_namespace("od5a_run_")
    with (
        PostgresPersistence(DSN, namespace=ref_ns) as oracle,
        PostgresPersistence(DSN, namespace=run_ns) as actual,
    ):
        for org in ORGS:
            initialize_od5a_durable(
                oracle, job_id=org, arm=_arm(), items=_items(org),
            )
            initialize_od5a_recurring_job(
                actual, job_id=org, arm=_arm(), items=_items(org),
            )

    ctx = get_context("spawn")
    requests = {org: ctx.Queue() for org in ORGS}
    answers = {org: ctx.Queue() for org in ORGS}
    workers = {
        org: ctx.Process(
            target=_process_loop,
            args=(DSN, run_ns, org, requests[org], answers[org]),
        ) for org in ORGS
    }
    completed = set()
    action_traces = {org: [] for org in ORGS}
    try:
        for process in workers.values():
            process.start()
        for index in range(150):
            active = tuple(org for org in ORGS if org not in completed)
            if not active:
                break
            now = NOW + timedelta(seconds=index)
            # G2 separately demonstrates actual simultaneous PostgreSQL
            # boundary critical sections. G3 must instead check after EACH
            # committed transition, not only after a pair that might mask
            # a transient cross-organization mutation. Both OS workers stay
            # independent; the observer lets only one commit at a time.
            for org in active:
                requests[org].put(("owner:" + org, now))
                action, error = answers[org].get(timeout=50)
                assert error is None, error
                with PostgresPersistence(DSN, namespace=ref_ns) as oracle:
                    reference = advance_od5a_reference_occurrence(
                        oracle, job_id=org, owner_id="owner:" + org,
                        now=now, lease_duration=LEASE,
                    )
                assert action == reference.action
                action_traces[org].append(action)
                if action == "complete":
                    completed.add(org)
                with (
                    PostgresPersistence(DSN, namespace=ref_ns) as oracle,
                    PostgresPersistence(DSN, namespace=run_ns) as actual,
                ):
                    assert _certificate(actual, ORGS) == _certificate(oracle, ORGS)
                    for independent_org in ORGS:
                        checkpoint = actual.job_state(independent_org)
                        with actual.transaction() as uow:
                            source = uow.get_entity(
                                "od5a_durable_pilot", independent_org,
                            )
                        assert checkpoint.run_count == source.attributes["step"]
                        assert checkpoint.next_tick == source.attributes["step"]
                        assert checkpoint.logical_time == source.updated_at
        else:
            pytest.fail("independent PostgreSQL organization processes did not converge")
        assert completed == set(ORGS)
        for org in ORGS:
            assert {"source_advanced", "published", "claimed", "acknowledged",
                    "materialized", "certified", "complete"} <= set(action_traces[org])
        with PostgresPersistence(DSN, namespace=run_ns) as recovered:
            assert recovered.resource_reservations() == ()
    finally:
        for queue in requests.values():
            queue.put(None)
        for process in workers.values():
            process.join(timeout=15)
            if process.is_alive():
                process.kill()
                process.join()
        for queue in (*requests.values(), *answers.values()):
            queue.close()
            queue.join_thread()


def test_foreign_destination_cannot_steal_od5a_physical_delivery():
    namespace = _pg_namespace("od5a_foreign_")
    org = "org:isolated"
    with PostgresPersistence(DSN, namespace=namespace) as store:
        initialize_od5a_recurring_job(
            store, job_id=org, arm=_arm(), items=_items(org),
        )
        unrelated = BoundaryMessage.create(
            contract_name="inventory.not_od5a", contract_version=1,
            source_domain="inventory", source_identity="inventory-1",
            destination_domain="unrelated_domain",
            occurrence_key="foreign-event", correlation_id=org,
            causation_id=None, produced_at=NOW, payload={"item": "alien"},
        )
        foreign_delivery = BoundaryService(store).publish(unrelated)
        for _ in range(120):
            result = _integrated(store, org, "owner:isolated", NOW)
            if result.action == "complete":
                break
        else:
            pytest.fail("foreign delivery blocked OD-5a completion")
        with store.transaction() as uow:
            row = uow.get_boundary_delivery(foreign_delivery.delivery_id)
            assert row.status is DeliveryStatus.PENDING
            assert uow.get_boundary_consumption(foreign_delivery.delivery_id) is None
            assert uow.get_business_effect(foreign_delivery.delivery_id) is None
