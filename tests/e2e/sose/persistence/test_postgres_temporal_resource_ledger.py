"""Authoritative PostgreSQL resource ledger: concurrent capacity, outage and restart.

These tests do not mistake an advisory mutex for a durable allocation. Every
proof reconstructs resource records from PostgreSQL after closing the writer.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from threading import Barrier, Event, Thread
from uuid import uuid4
import os

import pytest

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import (
    ResourceCapacityError, ResourceConflictError,
    TemporalReservation, TemporalOutage,
)
from sose.persistence.postgres import PostgresPersistence
from sose.core.events import DomainEvent
from sose.domain.entity import Entity

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
T0 = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
H = timedelta(hours=1)


def _namespace(prefix):
    return prefix + "_" + uuid4().hex[:12]


def _pool(*, named=False, scope="shared", org=None):
    return ResourcePoolContract(
        resource_type="forklift" if named else "service_processor",
        pool_id="site-a", scope=scope, organization_id=org,
        capacity=2, instance_ids=("unit-1", "unit-2") if named else (),
    )


def _request(pool, rid, start=0, end=4, *, instance=None, units=1,
             priority=100, owner="order"):
    return TemporalReservation(
        reservation_id=rid, address=pool.address(instance_id=instance),
        owner_id=owner, start_at=T0 + start * H,
        end_at=T0 + end * H, units=units, priority=priority,
        causation_id=f"order:{owner}",
    )


def test_pg_three_competing_requests_cannot_exceed_two_finite_slots():
    assert DSN
    ns = _namespace("capacity")
    pool = _pool()
    with PostgresPersistence(DSN, namespace=ns) as db:
        db.temporal_resources().register_pool(pool)
    gate = Barrier(3)

    def worker(index):
        with PostgresPersistence(DSN, namespace=ns) as db:
            ledger = db.temporal_resources()
            gate.wait(timeout=12)
            try:
                ledger.reserve(_request(pool, f"job-{index}", 0, 4))
                return True
            except ResourceCapacityError:
                return False

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(worker, range(3)))
    assert results.count(True) == 2, results
    assert results.count(False) == 1, results
    with PostgresPersistence(DSN, namespace=ns) as reopened:
        ledger = reopened.temporal_resources()
        assert len(ledger.reservations()) == 2
        assert len(ledger.events()) == 2
        assert len(ledger.audit()) == 64


def test_pg_all_change_points_and_touching_intervals_are_respected():
    assert DSN
    ns = _namespace("timeline")
    pool = _pool()
    with PostgresPersistence(DSN, namespace=ns) as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        ledger.reserve(_request(pool, "a", 0, 3))
        ledger.reserve(_request(pool, "b", 2, 4))
        # At t=3 the first reservation has ended; [3,4) has one free slot.
        ledger.reserve(_request(pool, "c", 3, 5))
        with pytest.raises(ResourceCapacityError):
            ledger.reserve(_request(pool, "d", 2, 3))
        with pytest.raises(ResourceConflictError):
            ledger.reserve(_request(pool, "b", 2, 4, units=2))
        assert len(ledger.reservations()) == len(ledger.events()) == 3
        assert ledger.audit()


def test_pg_physical_instance_failure_preemption_and_recovery():
    assert DSN
    ns = _namespace("physical")
    pool = _pool(named=True)
    with PostgresPersistence(DSN, namespace=ns) as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        victim = _request(pool, "low", 0, 5, instance="unit-1", priority=100)
        ledger.reserve(victim)
        with pytest.raises(ResourceCapacityError):
            ledger.reserve(_request(pool, "conflict", 1, 2, instance="unit-1"))
        high = _request(pool, "high", 2, 4, instance="unit-1", priority=1)
        ledger.reserve(high, preempt=True)
        assert ledger.get("low").status == "preempted"
        assert ledger.get("low").stopped_at == T0 + 2 * H
        assert ledger.reserve(high, preempt=True) == high
        # Another physical instance in the same pool can work concurrently.
        ledger.reserve(_request(pool, "other", 2, 5, instance="unit-2"))
        outage = TemporalOutage(
            "unit-1-failure", pool.address(instance_id="unit-1"),
            T0 + 3 * H, T0 + 6 * H,
        )
        ledger.fail(outage)
        assert ledger.get("high").status == "failed"
        assert ledger.get("high").stopped_at == T0 + 3 * H
        with pytest.raises(ResourceCapacityError):
            ledger.reserve(_request(pool, "broken", 3, 4, instance="unit-1"))
        assert ledger.get("other").status == "reserved"
        ledger.recover("unit-1-failure", at=T0 + 4 * H)
        ledger.reserve(_request(pool, "after-recovery", 4, 6, instance="unit-1"))
        assert ledger.audit()
    with PostgresPersistence(DSN, namespace=ns) as reopened:
        ledger = reopened.temporal_resources()
        assert {x.status for x in ledger.reservations()} == {
            "reserved", "preempted", "failed",
        }
        assert len(ledger.outages()) == 1
        assert ledger.outages()[0].recovered_at == T0 + 4 * H
        assert ledger.audit()


def test_pg_release_retries_and_calendar_outages_are_durable():
    assert DSN
    ns = _namespace("release")
    pool = _pool()
    with PostgresPersistence(DSN, namespace=ns) as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        ledger.reserve(_request(pool, "a", 0, 5, units=2))
        ledger.release("a", at=T0 + 2 * H)
        assert ledger.release("a", at=T0 + 2 * H).status == "released"
        with pytest.raises(ResourceConflictError):
            ledger.release("a", at=T0 + 3 * H)
        outage = TemporalOutage("maintenance", pool.address(),
                                 T0 + 2 * H, T0 + 5 * H)
        ledger.fail(outage)
        with pytest.raises(ResourceCapacityError):
            ledger.reserve(_request(pool, "blocked", 3, 4))
        ledger.recover("maintenance", at=T0 + 4 * H)
        with pytest.raises(ResourceConflictError):
            ledger.recover("maintenance", at=T0 + 3 * H)
        ledger.reserve(_request(pool, "after", 4, 6, units=2))
        assert ledger.audit()


def _die_after_commit(dsn, namespace):
    """Child deliberately exits without graceful cleanup after COMMIT."""
    with PostgresPersistence(dsn, namespace=namespace) as db:
        pool = _pool()
        ledger = db.temporal_resources()
        ledger.reserve(_request(pool, "b", 0, 4))
        os._exit(17)


def _experiment(namespace: str, *, kill_worker: bool):
    assert DSN
    pool = _pool()
    with PostgresPersistence(DSN, namespace=namespace) as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        ledger.reserve(_request(pool, "a", 0, 4, priority=100))
        if not kill_worker:
            ledger.reserve(_request(pool, "b", 0, 4, priority=100))
    if kill_worker:
        worker = get_context("spawn").Process(
            target=_die_after_commit, args=(DSN, namespace),
        )
        worker.start()
        worker.join(timeout=20)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
            pytest.fail("worker did not terminate within bounded time")
        assert worker.exitcode == 17

    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        ledger = recovered.temporal_resources()
        # Command retry after lost acknowledgement must not duplicate business truth.
        assert ledger.reserve(_request(pool, "b", 0, 4, priority=100)).reservation_id == "b"
        ledger.reserve(_request(pool, "high", 2, 3, priority=1), preempt=True)
        ledger.release("b", at=T0 + 3 * H)
        ledger.fail(TemporalOutage(
            "maintenance", pool.address(), T0 + 3 * H, T0 + 5 * H,
        ))
        ledger.recover("maintenance", at=T0 + 4 * H)
        ledger.reserve(_request(pool, "post-repair", 4, 6, units=2))
        digest = ledger.audit()
        snapshot = ledger.snapshot()
    with PostgresPersistence(DSN, namespace=namespace) as second_recovery:
        ledger = second_recovery.temporal_resources()
        assert ledger.audit() == digest
        assert ledger.snapshot() == snapshot
    return digest, snapshot


def test_pg_worker_sigkill_restart_is_causally_equivalent_to_continuous_reference():
    assert DSN
    baseline = _experiment(_namespace("baseline"), kill_worker=False)
    killed = _experiment(_namespace("faulted"), kill_worker=True)
    assert baseline == killed, "worker death changed durable resource business causality"


def test_pg_domain_event_and_allocation_share_transactional_commit():
    """An outer Engine UoW can atomically commit domain truth plus resource truth."""
    assert DSN
    ns = _namespace("coupled")
    pool = _pool()
    with PostgresPersistence(DSN, namespace=ns) as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        with pytest.raises(RuntimeError, match="injected before commit"):
            with db.transaction() as uow:
                ledger.reserve(_request(pool, "rolled-back", 0, 2))
                uow.save_entity(Entity(
                    id="work-1", entity_type="work_order", state="processing",
                ))
                uow.append_event(DomainEvent(
                    event_id="work-1:started", name="work.started",
                    entity_type="work_order", entity_id="work-1", occurred_at=T0,
                    causation_id="reserve:rolled-back",
                ))
                raise RuntimeError("injected before commit")
        assert ledger.get("rolled-back") is None
        assert db.entity("work_order", "work-1") is None
        assert db.events() == ()
        with db.transaction() as uow:
            ledger.reserve(_request(pool, "committed", 0, 2))
            uow.save_entity(Entity(
                id="work-1", entity_type="work_order", state="processing",
            ))
            uow.append_event(DomainEvent(
                event_id="work-1:started", name="work.started",
                entity_type="work_order", entity_id="work-1", occurred_at=T0,
                causation_id="reserve:committed",
            ))
    with PostgresPersistence(DSN, namespace=ns) as reopened:
        ledger = reopened.temporal_resources()
        assert len(ledger.reservations()) == 1
        assert ledger.get("committed") is not None
        assert reopened.entity("work_order", "work-1").state == "processing"
        assert len(reopened.events()) == 1
        assert reopened.events()[0].causation_id == "reserve:committed"
        assert ledger.audit()


def test_pg_distinct_organization_pools_commit_while_other_transaction_open():
    """No namespace-wide resource mutex may serialize unrelated organizations."""
    assert DSN
    ns = _namespace("disjoint")
    local_a = _pool(scope="organization", org="factory-a")
    local_b = _pool(scope="organization", org="factory-b")
    with (
        PostgresPersistence(DSN, namespace=ns) as first,
        PostgresPersistence(DSN, namespace=ns) as second,
    ):
        a = first.temporal_resources()
        b = second.temporal_resources()
        a.register_pool(local_a)
        b.register_pool(local_b)
        held = Event()
        release = Event()
        finished_b = Event()
        errors = []

        def reserve_a_with_open_transaction():
            try:
                with first.transaction():
                    a.reserve(_request(local_a, "org-a", 0, 2))
                    held.set()
                    if not release.wait(10):
                        raise TimeoutError("owner not released")
            except BaseException as exc:
                errors.append(exc)

        def reserve_b_independently():
            try:
                assert held.wait(10)
                b.reserve(_request(local_b, "org-b", 0, 2))
                finished_b.set()
            except BaseException as exc:
                errors.append(exc)

        x = Thread(target=reserve_a_with_open_transaction)
        y = Thread(target=reserve_b_independently)
        x.start()
        y.start()
        try:
            assert held.wait(10)
            assert finished_b.wait(5), "unrelated scoped resource blocked by global lock"
        finally:
            release.set()
            x.join(timeout=10)
            y.join(timeout=10)
        assert not x.is_alive() and not y.is_alive()
        assert not errors
    with PostgresPersistence(DSN, namespace=ns) as reopened:
        ledger = reopened.temporal_resources()
        assert {x.reservation_id for x in ledger.reservations()} == {"org-a", "org-b"}
        assert ledger.audit()
