"""Authoritative PostgreSQL resource ledger: concurrent capacity, outage and restart.

These tests do not mistake an advisory mutex for a durable allocation. Every
proof reconstructs resource records from PostgreSQL after closing the writer.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from threading import Barrier, Event, Thread
from uuid import uuid4
import os

import pytest
from psycopg import sql

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


def test_pg_shared_physical_pool_is_one_authoritative_ledger_for_independent_owners():
    """#477: cross-organization physical grants do not duplicate a real unit.

    This proves PostgreSQL authority at the shared temporal-pool boundary.
    OD-5a recurring source jobs still use owner-local SimPy resources; this
    test does NOT certify automatic cross-owner source-to-pool admission.
    """
    assert DSN is not None
    ns = _namespace("od5a_shared_owner")
    pool = ResourcePoolContract(
        resource_type="execution_technician", pool_id="site-a",
        scope="shared", capacity=2, instance_ids=("unit-1", "unit-2"),
    )
    with PostgresPersistence(DSN, namespace=ns) as store:
        store.temporal_resources().register_pool(pool)

    gate = Barrier(3)
    proposals = (
        ("org:a", "unit-1"),
        ("org:b", "unit-1"),
        ("org:c", "unit-2"),
    )
    submitted = {
        owner: _request(
            pool, f"physical-grant:{owner}",
            0, 5, instance=instance, owner=owner,
        )
        for owner, instance in proposals
    }

    def assert_preemption_ancestry(ledger, victim_id):
        event_id = f"preempt:higher-priority-cross-owner:{victim_id}"
        matches = [e for e in ledger.events() if e[0] == event_id]
        assert len(matches) == 1, "one durable preemption event expected"
        event = matches[0]
        assert (event[2], event[3], event[4], event[5]) == (
            victim_id, "preempted", T0 + 2 * H,
            "reserve:higher-priority-cross-owner",
        ), "displaced booking must directly cite the priority grant"

    def attempt(args):
        owner, instance = args
        with PostgresPersistence(DSN, namespace=ns) as writer:
            gate.wait(timeout=15)
            try:
                writer.temporal_resources().reserve(submitted[owner])
                return owner, "granted"
            except ResourceCapacityError:
                return owner, "wait-capacity"

    with ThreadPoolExecutor(max_workers=3) as workers:
        outcomes = dict(workers.map(attempt, proposals))

    assert outcomes["org:c"] == "granted", outcomes
    assert {outcomes["org:a"], outcomes["org:b"]} == {
        "granted", "wait-capacity",
    }, outcomes

    with PostgresPersistence(DSN, namespace=ns) as restarted:
        ledger = restarted.temporal_resources()
        reservations = ledger.reservations()
        assert len(reservations) == 2
        assert len(ledger.events()) == 2
        assert {r.owner_id for r in reservations} == {
            "org:c", next(o for o in ("org:a", "org:b")
                           if outcomes[o] == "granted"),
        }
        assert {r.address.instance_id for r in reservations} == {
            "unit-1", "unit-2",
        }
        assert all(r.status == "reserved" for r in reservations)
        assert all(
            r == submitted[r.owner_id] for r in reservations
        ), "persisted grant must equal the original immutable owner/instance request"
        assert ledger.get(submitted[next(
            owner for owner in ("org:a", "org:b")
            if outcomes[owner] == "wait-capacity"
        )].reservation_id) is None, "rejected booking cannot appear in ledger"
        assert ledger.audit(), "source-of-truth temporal ledger must audit"
        victim = next(r for r in reservations
                      if r.address.instance_id == "unit-1")
        rejected_owner = next(o for o in ("org:a", "org:b")
                              if outcomes[o] == "wait-capacity")
        replacement = _request(
            pool, "higher-priority-cross-owner",
            2, 4, instance="unit-1", owner=rejected_owner, priority=1,
        )
        ledger.reserve(replacement, preempt=True)
        assert ledger.get(victim.reservation_id).status == "preempted"
        assert ledger.get(victim.reservation_id).stopped_at == T0 + 2 * H
        assert ledger.get("higher-priority-cross-owner").owner_id == rejected_owner
        # Preempting the contested physical unit must not rewrite the second
        # organization's independent reservation or corrupt pool capacity.
        assert ledger.get("physical-grant:org:c").status == "reserved"
        assert len(ledger.events()) == 4
        assert_preemption_ancestry(ledger, victim.reservation_id)
        assert ledger.audit()

    with PostgresPersistence(DSN, namespace=ns) as final:
        ledger = final.temporal_resources()
        assert ledger.get("higher-priority-cross-owner") == replacement
        assert ledger.get(victim.reservation_id).status == "preempted"
        assert ledger.get("physical-grant:org:c").status == "reserved"
        assert len(ledger.reservations()) == 3
        assert_preemption_ancestry(ledger, victim.reservation_id)
        assert ledger.audit()


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
        owner_id="temporal-ledger-test-organization",
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
        owner_id="temporal-ledger-test-organization",
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


def test_pg_prospective_slot_advice_preserves_past_outage_and_all_ledger_truth():
    """#477: ending an outage later cannot retroactively allocate its past."""
    assert DSN
    ns = _namespace("advice_outage")
    pool = _pool(named=True)
    with PostgresPersistence(DSN, namespace=ns) as writer:
        ledger = writer.temporal_resources()
        ledger.register_pool(pool)
        ledger.reserve(_request(pool, "occupied", 0, 2, instance="unit-1", owner="org:a"))
        ledger.fail(TemporalOutage(
            outage_id="broken-unit", address=pool.address(instance_id="unit-1"),
            start_at=T0 + 2 * H, end_at=T0 + 4 * H,
        ))
        wait = _request(pool, "pending", 0, 1, instance="unit-1", owner="org:b")
        other = _request(pool, "independent", 0, 1, instance="unit-2", owner="org:c")
        before = ledger.snapshot()
        assert ledger.suggest_next_start(
            wait, not_before=T0, latest_start=T0 + 3 * H,
        ) is None
        assert ledger.suggest_next_start(
            wait, not_before=T0, latest_start=T0 + 5 * H,
        ) == T0 + 4 * H
        assert ledger.suggest_next_start(
            other, not_before=T0, latest_start=T0,
        ) == T0
        assert ledger.suggest_next_start(
            wait, not_before=T0 + 5 * H, latest_start=T0 + 5 * H,
        ) == T0 + 5 * H
        assert ledger.snapshot() == before, "advice must not book, emit or change physical truth"
        assert ledger.get("pending") is None
        # Source-authorized *future* reservation is a separate write.
        assert ledger.reserve(replace(
            wait, start_at=T0 + 4 * H, end_at=T0 + 5 * H,
        )).start_at == T0 + 4 * H
        assert ledger.audit()
    with PostgresPersistence(DSN, namespace=ns) as recovered:
        ledger = recovered.temporal_resources()
        assert {r.reservation_id for r in ledger.reservations()} == {
            "occupied", "pending",
        }
        assert ledger.get("pending").start_at == T0 + 4 * H
        assert ledger.outages()[0].effective_end == T0 + 4 * H
        assert ledger.audit()


def test_pg_prospective_start_handles_fungible_capacity_change_points_and_recovery():
    """Only durable end boundaries can create additional capacity."""
    assert DSN
    ns = _namespace("advice_capacity")
    pool = _pool()
    with PostgresPersistence(DSN, namespace=ns) as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        ledger.reserve(_request(pool, "one", 0, 2, owner="org:a"))
        ledger.reserve(_request(pool, "two", 1, 4, owner="org:b"))
        waiting = _request(pool, "three", 0, 2, owner="org:c")
        assert ledger.suggest_next_start(
            waiting, not_before=T0, latest_start=T0 + H,
        ) is None
        assert ledger.suggest_next_start(
            waiting, not_before=T0, latest_start=T0 + 3 * H,
        ) == T0 + 2 * H
        assert ledger.suggest_next_start(
            _request(pool, "oversized", 0, 1, units=3),
            not_before=T0, latest_start=T0 + 6 * H,
        ) is None
        ledger.release("one", at=T0 + H)
        # The release at 1h frees a unit prospectively. The request for
        # only ONE unit can now fit at time zero: the pool has two units,
        # only one of which was ever occupied during [0,1h).
        assert ledger.suggest_next_start(
            waiting, not_before=T0, latest_start=T0 + 3 * H,
        ) == T0
        # A TWO-unit request cannot retroactively erase that one occupied
        # unit during [0,1h); after 1h the other owner's reservation still
        # occupies one unit, so capacity-2 never fits before 4h.
        two_units = _request(pool, "requires-both", 0, 1, units=2)
        assert ledger.suggest_next_start(
            two_units, not_before=T0, latest_start=T0 + 3 * H,
        ) is None
        assert ledger.suggest_next_start(
            two_units, not_before=T0, latest_start=T0 + 4 * H,
        ) == T0 + 4 * H
        assert ledger.audit()


def test_pg_prospective_hint_is_stale_after_other_owner_books_real_capacity():
    """A suggestion is never a permit; source/physical UoW must retry admission."""
    assert DSN
    ns = _namespace("advice_stale")
    pool = _pool(named=True)
    with PostgresPersistence(DSN, namespace=ns) as first:
        ledger = first.temporal_resources()
        ledger.register_pool(pool)
        ledger.reserve(_request(pool, "incumbent", 0, 2, instance="unit-1", owner="a"))
        waiting = _request(pool, "pending", 0, 1, instance="unit-1", owner="b")
        suggestion = ledger.suggest_next_start(
            waiting, not_before=T0, latest_start=T0 + 3 * H,
        )
        assert suggestion == T0 + 2 * H
    with PostgresPersistence(DSN, namespace=ns) as contender:
        contender.temporal_resources().reserve(
            _request(pool, "other-winner", 2, 3, instance="unit-1", owner="c")
        )
    with PostgresPersistence(DSN, namespace=ns) as attempted:
        ledger = attempted.temporal_resources()
        with pytest.raises(ResourceCapacityError, match="finite resource unavailable"):
            ledger.reserve(replace(
                waiting, start_at=suggestion, end_at=suggestion + H,
            ))
        assert ledger.get("pending") is None
        assert ledger.suggest_next_start(
            waiting, not_before=T0 + 2 * H, latest_start=T0 + 4 * H,
        ) == T0 + 3 * H
        assert ledger.audit()


def test_pg_prospective_advice_rejects_wrong_identity_or_retroactive_window():
    assert DSN
    ns = _namespace("advice_invalid")
    pool = _pool(named=True)
    bad = _request(pool, "forbidden", 2, 3, instance="unit-1")
    with PostgresPersistence(DSN, namespace=ns) as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        with pytest.raises(ValueError, match="before original demand"):
            ledger.suggest_next_start(
                bad, not_before=T0 + H, latest_start=T0 + 6 * H,
            )
        with pytest.raises(ValueError, match="precedes"):
            ledger.suggest_next_start(
                bad, not_before=T0 + 4 * H, latest_start=T0 + 3 * H,
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            ledger.suggest_next_start(
                bad, not_before=T0.replace(tzinfo=None), latest_start=T0 + 6 * H,
            )
        ledger.reserve(bad)
        with pytest.raises(ResourceConflictError, match="already-booked"):
            ledger.suggest_next_start(
                bad, not_before=bad.start_at, latest_start=T0 + 4 * H,
            )


def test_pg_advice_inside_outer_sql_transaction_does_not_fence_pool_writers():
    """#477 P1: nested hint must not strand a transaction advisory lock."""
    assert DSN
    ns = _namespace("advice_outer")
    pool = _pool(named=True)
    with PostgresPersistence(DSN, namespace=ns) as seed:
        seed.temporal_resources().register_pool(pool)
    with PostgresPersistence(DSN, namespace=ns) as reader, \
            PostgresPersistence(DSN, namespace=ns) as writer:
        # Ledger construction performs first-time DDL bootstrap under a
        # namespace-wide advisory lock. Construct BOTH instances *before*
        # the deliberately open SQL transaction; otherwise the test would
        # deadlock the bootstrap rather than falsify the query's lock scope.
        reader_ledger = reader.temporal_resources()
        writer_ledger = writer.temporal_resources()
        writer._connection.execute("SET lock_timeout TO '3000ms'")
        wait = _request(pool, "uncommitted-hint", 0, 1, instance="unit-1", owner="reader")
        other = _request(pool, "independent-commit", 0, 1, instance="unit-1", owner="writer")
        with reader._connection.transaction():
            assert reader_ledger.suggest_next_start(
                wait, not_before=T0, latest_start=T0 + H,
            ) == T0
            # Outer transaction is intentionally still open while another
            # PostgreSQL connection MUST finish its real reserve. No pool
            # advisory lock may outlive the purely advisory query.
            with ThreadPoolExecutor(max_workers=1) as workers:
                result = workers.submit(writer_ledger.reserve, other)
                assert result.result(timeout=10) == other
        with pytest.raises(ResourceCapacityError):
            reader_ledger.reserve(wait)
        assert reader_ledger.get("uncommitted-hint") is None
        assert reader_ledger.get("independent-commit") == other
        assert reader_ledger.audit()


def test_pg_advice_omits_unrelated_historical_intervals_from_window(monkeypatch):
    """#477 P2: no full-history conversion under a short advice horizon."""
    assert DSN
    ns = _namespace("advice_bounded")
    pool = _pool(named=True)
    with PostgresPersistence(DSN, namespace=ns) as store:
        ledger = store.temporal_resources()
        ledger.register_pool(pool)
        for idx in range(24):
            ledger.reserve(TemporalReservation(
                reservation_id=f"historical:{idx}", owner_id="archived",
                address=pool.address(instance_id="unit-1"),
                start_at=T0 - timedelta(hours=100 - idx * 2),
                end_at=T0 - timedelta(hours=99 - idx * 2),
                causation_id=f"history:{idx}",
            ))
        ledger.fail(TemporalOutage(
            outage_id="historic-down", address=pool.address(instance_id="unit-1"),
            start_at=T0 - 10 * H, end_at=T0 - 9 * H,
        ))
        ledger.reserve(_request(
            pool, "current", 1, 2, instance="unit-1", owner="busy",
        ))
        seen = {}
        book_reader = ledger._window_bookings
        outage_reader = ledger._window_outages

        def read_bookings(*args, **kwargs):
            values = book_reader(*args, **kwargs)
            seen["bookings"] = tuple(x.reservation_id for x in values)
            return values

        def read_outages(*args, **kwargs):
            values = outage_reader(*args, **kwargs)
            seen["outages"] = tuple(x.outage_id for x in values)
            return values

        monkeypatch.setattr(ledger, "_window_bookings", read_bookings)
        monkeypatch.setattr(ledger, "_window_outages", read_outages)
        waiting = _request(pool, "query", 0, 1, instance="unit-1", owner="query")
        assert ledger.suggest_next_start(
            waiting, not_before=T0, latest_start=T0 + H,
        ) == T0
        assert seen == {"bookings": ("current",), "outages": ()}
        assert ledger.get("query") is None
        # With only 25 rows, PostgreSQL may legitimately choose the older
        # pool-key B-tree even when GiST is usable. Assert both installed
        # index EXPRESSIONS here; the future-heavy benchmark below requires
        # actual GiST selection and low scanned rows in EXPLAIN ANALYZE.
        for name, effective in (
            (f"{ns}_rb_window_gist", "COALESCE(stopped_at, end_at)"),
            (f"{ns}_ro_window_gist", "COALESCE(recovered_at, end_at)"),
        ):
            indexed = ledger._db.execute("""
                SELECT indexdef FROM pg_indexes
                WHERE schemaname = current_schema() AND indexname = %s
            """, (name,)).fetchone()
            assert indexed is not None and "USING gist" in indexed[0], indexed
            assert "tstzrange(start_at" in indexed[0], indexed
            assert effective in indexed[0], indexed
        assert ledger.audit()


def test_pg_instant_outage_recovery_is_zero_length_in_advice_and_real_reserve():
    """The effective empty outage [t,t) must block neither grant nor hint."""
    assert DSN
    ns = _namespace("advice_instant_recovery")
    pool = _pool(named=True)
    with PostgresPersistence(DSN, namespace=ns) as writer:
        ledger = writer.temporal_resources()
        ledger.register_pool(pool)
        outage = TemporalOutage(
            outage_id="down-then-instant-up",
            address=pool.address(instance_id="unit-1"),
            start_at=T0 + H, end_at=T0 + 3 * H,
        )
        ledger.fail(outage)
        recovered = ledger.recover(outage.outage_id, at=outage.start_at)
        assert recovered.effective_end == recovered.start_at
        pending = _request(pool, "spans-instant-recovery", 0, 2,
                           instance="unit-1", owner="org:test")
        snapshot_before = ledger.snapshot()
        assert ledger.suggest_next_start(
            pending, not_before=T0, latest_start=T0,
        ) == T0
        assert ledger.snapshot() == snapshot_before
        assert ledger.reserve(pending) == pending
        assert ledger.audit()
    with PostgresPersistence(DSN, namespace=ns) as reopened:
        ledger = reopened.temporal_resources()
        assert ledger.get("spans-instant-recovery") == pending
        assert ledger.outages()[0].effective_end == T0 + H
        assert ledger.audit()


def test_pg_prospective_gist_window_prunes_distant_future_heavy_history():
    """#477 P2: prove actual bounded GiST scans, not just named indexes.

    Synthetic distant-future rows are injected ONLY within a deliberately
    rolled-back SQL transaction to probe planner complexity. They are never
    published as authoritative grants without causal events.
    """
    assert DSN
    ns = _namespace("advice_gist_future")
    pool = _pool(named=True)

    class RollbackPerformanceFixture(Exception):
        pass

    def walk(plan):
        yield plan
        for child in plan.get("Plans", ()):
            yield from walk(child)

    with PostgresPersistence(DSN, namespace=ns) as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        wait = _request(pool, "pending", 0, 1, instance="unit-1")
        try:
            with db._connection.transaction():
                # Test-only rows: capacity bookkeeping is intentionally not
                # certified and the entire SQL transaction MUST roll back.
                # Place 1500 bookings and 1500 outages beyond the tiny query
                # window to falsify a B-tree that scans all future tails.
                db._connection.execute(sql.SQL("""
                    INSERT INTO {} (
                        reservation_id, pool_key, instance_id, owner_id,
                        start_at, end_at, units, priority, status,
                        stopped_at, causation_id
                    )
                    SELECT 'test-future-booking:' || g, %s, 'unit-1', 'fixture',
                           %s::timestamptz + g * interval '1 hour',
                           %s::timestamptz + g * interval '1 hour'
                             + interval '30 minutes',
                           1, 100, 'reserved', NULL, 'test-only'
                    FROM generate_series(1, 1500) AS g
                """).format(ledger._reservations), (
                    pool.address().lock_key(), T0 + timedelta(days=30),
                    T0 + timedelta(days=30),
                ))
                db._connection.execute(sql.SQL("""
                    INSERT INTO {} (
                        outage_id, pool_key, instance_id,
                        start_at, end_at, recovered_at
                    )
                    SELECT 'test-future-outage:' || g, %s, 'unit-1',
                           %s::timestamptz + g * interval '1 hour',
                           %s::timestamptz + g * interval '1 hour'
                             + interval '30 minutes',
                           NULL
                    FROM generate_series(1, 1500) AS g
                """).format(ledger._outages), (
                    pool.address().lock_key(), T0 + timedelta(days=30),
                    T0 + timedelta(days=30),
                ))
                # Planner statistics reflect the future-heavy fixture.
                db._connection.execute(sql.SQL("ANALYZE {}").format(ledger._reservations))
                db._connection.execute(sql.SQL("ANALYZE {}").format(ledger._outages))
                db._connection.execute("SET LOCAL enable_seqscan=off")
                assert ledger.suggest_next_start(
                    wait, not_before=T0, latest_start=T0 + 2 * H,
                ) == T0

                for table, key, expression, index in (
                    (ledger._reservations, "reservation_id",
                     "COALESCE(stopped_at, end_at)", f"{ns}_rb_window_gist"),
                    (ledger._outages, "outage_id",
                     "COALESCE(recovered_at, end_at)", f"{ns}_ro_window_gist"),
                ):
                    plan = db._connection.execute(sql.SQL("""
                        EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                        SELECT {} FROM {}
                        WHERE pool_key = %s
                          AND tstzrange(start_at, {}, '[)')
                              && tstzrange(%s, %s, '[)')
                    """).format(
                        sql.Identifier(key), table, sql.SQL(expression),
                    ), (pool.address().lock_key(), T0, T0 + 3 * H)).fetchone()[0]
                    if isinstance(plan, str):
                        import json
                        plan = json.loads(plan)
                    tree = plan[0]["Plan"]
                    nodes = tuple(walk(tree))
                    indexed = [node for node in nodes if node.get("Index Name") == index]
                    assert indexed, nodes
                    assert all("&&" in node.get("Index Cond", "") for node in indexed)
                    # No future rows may reach the heap or a post-index filter.
                    assert tree["Actual Rows"] == 0, tree
                    assert all(node.get("Rows Removed by Filter", 0) == 0
                               for node in nodes), nodes
                raise RollbackPerformanceFixture
        except RollbackPerformanceFixture:
            pass

        # No test-only uncaused booking or outage can survive its SQL rollback.
        assert ledger.reservations() == ()
        assert ledger.outages() == ()
        assert ledger.get("pending") is None
        assert ledger.audit()


def test_pg_prospective_outages_use_pool_index_amid_unrelated_same_time_outages():
    """#477: simultaneous outages of other pools must not become a scan tax.

    All 1500 foreign pool/outage records are synthetic and rolled back before
    audit; they are never authoritative resource facts or events.
    """
    assert DSN
    ns = _namespace("advice_other_pools")
    pool = _pool(named=True)

    class RollbackPerformanceFixture(Exception):
        pass

    def walk(node):
        yield node
        for child in node.get("Plans", ()):
            yield from walk(child)

    with PostgresPersistence(DSN, namespace=ns) as db:
        ledger = db.temporal_resources()
        ledger.register_pool(pool)
        request = _request(pool, "target", 0, 1, instance="unit-1")
        try:
            with db._connection.transaction():
                db._connection.execute(sql.SQL("""
                    INSERT INTO {} (pool_key, definition)
                    SELECT 'test-foreign-pool:' || g, '{{}}'
                    FROM generate_series(1, 1500) AS g
                """).format(ledger._pools))
                db._connection.execute(sql.SQL("""
                    INSERT INTO {} (
                        outage_id, pool_key, instance_id,
                        start_at, end_at, recovered_at
                    )
                    SELECT 'test-foreign-outage:' || g,
                           'test-foreign-pool:' || g, NULL,
                           %s::timestamptz,
                           %s::timestamptz,
                           NULL
                    FROM generate_series(1, 1500) AS g
                """).format(ledger._outages), (T0, T0 + H))
                db._connection.execute(sql.SQL("ANALYZE {}").format(ledger._outages))
                db._connection.execute("SET LOCAL enable_seqscan=off")
                assert ledger.suggest_next_start(
                    request, not_before=T0, latest_start=T0,
                ) == T0
                plan = db._connection.execute(sql.SQL("""
                    EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                    SELECT outage_id FROM {}
                    WHERE pool_key = %s
                      AND tstzrange(start_at, COALESCE(recovered_at, end_at), '[)')
                          && tstzrange(%s, %s, '[)')
                """).format(ledger._outages), (
                    pool.address().lock_key(), T0, T0 + H,
                )).fetchone()[0]
                if isinstance(plan, str):
                    import json
                    plan = json.loads(plan)
                nodes = tuple(walk(plan[0]["Plan"]))
                pool_index = f"{ns}_ro_pool_idx"
                selected = [
                    node for node in nodes
                    if node.get("Index Name") == pool_index
                ]
                assert selected, nodes
                assert any("pool_key" in node.get("Index Cond", "")
                           for node in selected), selected
                assert plan[0]["Plan"]["Actual Rows"] == 0
                # A BitmapAnd may read ALL 1500 matching timestamps through
                # the temporal GiST, then bitmap-AND those tuples with ZERO
                # pool-key hits and emit nothing at the root. Zero final
                # rows is thus insufficient evidence of bounded physical IO.
                # All leaf index scans (including Bitmap Index Scan) must
                # emit at most the actual target pool's own records (zero).
                scanned_indexes = [
                    node for node in nodes
                    if node.get("Node Type") in {
                        "Index Scan", "Index Only Scan", "Bitmap Index Scan",
                    }
                ]
                assert scanned_indexes, nodes
                assert all(node.get("Actual Rows", 0) == 0
                           for node in scanned_indexes), scanned_indexes
                assert all(node.get("Rows Removed by Filter", 0) == 0
                           for node in nodes), nodes
                raise RollbackPerformanceFixture
        except RollbackPerformanceFixture:
            pass

        # Test fixture never commits fictitious outage events or pools.
        assert ledger.outages() == ()
        assert {x["pool_id"] for _, x in ledger.snapshot()["pools"]} == {pool.pool_id}
        assert ledger.get("target") is None
        assert ledger.audit()
