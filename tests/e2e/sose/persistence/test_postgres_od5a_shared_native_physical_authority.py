"""#477: PostgreSQL-authoritative shared physical grants from the actual OD-5a SimPy source.

Real SQL transactions and the production native recurring entrypoint are
required. These are *not* sufficient to certify fairness, durable waiting,
network ambiguity, complete five-arm equivalence or scientific admission.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import (
    ResourceCapacityError, ResourceConflictError, authoritative_effect_reservation_id,
)
from sose.core.runtime import ResourceReservation
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational import od5a_recurring
from sose.organizational.od5a_recurring import (
    SharedPhysicalGrantAuthority, SharedPhysicalPoolBinding,
    initialize_od5a_recurring_job, run_od5a_recurring_tick,
)
from sose.organizational.od5a_durable import _ORIGIN, od5a_durable_state
from sose.persistence.postgres import PostgresPersistence
from sose.persistence.memory import MemoryPersistence
from tests.integration.sose.organizational.test_od5a_recurring_postgres import _arm


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="live SOSE_TEST_POSTGRES_DSN required")
NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


def _namespace() -> str:
    return "od5a_native_shared_" + uuid4().hex[:12]


def _pool():
    return ResourcePoolContract(
        resource_type="execution_technician", pool_id="site-a",
        scope="shared", capacity=2, instance_ids=("unit-1", "unit-2"),
    )


def _binding(instance: str) -> SharedPhysicalPoolBinding:
    return SharedPhysicalPoolBinding(
        pool=_pool(), execution_instances=(("execution:0", instance),),
    )


def _items(key: str):
    return (OD5aWorkItem(
        "item:" + key, 0.0,
        (("a", 0.2), ("b", 0.2), ("c", 0.1)),
    ),)


def _seed(store, org: str, binding: SharedPhysicalPoolBinding):
    return initialize_od5a_recurring_job(
        store, job_id=org, arm=_arm(), items=_items(org),
        shared_pool_binding=binding,
    )


def _tick(store, org: str, binding: SharedPhysicalPoolBinding):
    job = store.job_state(org)
    return run_od5a_recurring_tick(
        store, job_id=org, owner_id="worker:" + org,
        now=NOW, lease_duration=timedelta(seconds=60),
        trigger_id=deterministic_id(
            "od5a-recurring-tick", org, job.run_count + 1,
        ),
        shared_pool_binding=binding,
    )


def _external_id(local_id: str, org: str):
    return authoritative_effect_reservation_id(local_id, scope=org)


def _source(store, org: str):
    return od5a_durable_state(store, job_id=org)


def test_pg_two_independent_native_sources_grant_distinct_shared_instances_and_release():
    assert DSN
    ns = _namespace()
    a, b = _binding("unit-1"), _binding("unit-2")
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, "org:a", a)
        _seed(store, "org:b", b)
        assert _tick(store, "org:a", a).action == "advanced"
        assert _tick(store, "org:b", b).action == "advanced"

    with PostgresPersistence(DSN, namespace=ns) as reopened:
        ledger = reopened.temporal_resources()
        bookings = ledger.reservations()
        assert len(bookings) == 2
        assert {r.owner_id for r in bookings} == {"org:a", "org:b"}
        assert {r.address.instance_id for r in bookings} == {"unit-1", "unit-2"}
        assert all(r.status == "reserved" for r in bookings)
        for org, bound in (("org:a", a), ("org:b", b)):
            local = [r for r in reopened.resource_reservations()
                     if r.resource_name == f"{org}/execution:0"]
            assert len(local) == 1
            remote = ledger.get(_external_id(local[0].reservation_id, org))
            assert remote is not None and remote.owner_id == org
            assert remote.address == bound.pool.address(
                instance_id=bound.physical_instance("execution:0"),
            )
            assert remote.start_at == local[0].acquired_at == _ORIGIN
            assert remote.end_at == _ORIGIN + timedelta(seconds=0.2)
            assert remote.causation_id in {
                e.event_id for e in reopened.events() if e.owner_id == org
            }
            assert _source(reopened, org)["step"] == 1
        assert ledger.audit()
        before = ledger.snapshot()
        # Exact replay after lost response does not create another physical grant.
        trigger = deterministic_id("od5a-recurring-tick", "org:a", 1)
        replay = run_od5a_recurring_tick(
            reopened, job_id="org:a", owner_id="retry-worker",
            now=NOW, lease_duration=timedelta(seconds=60),
            trigger_id=trigger, shared_pool_binding=a,
        )
        assert replay.action == "replayed"
        assert ledger.snapshot() == before

        # Native completion and the physical ledger release commit together.
        assert _tick(reopened, "org:a", a).action == "advanced"
        released = ledger.get(next(
            r.reservation_id for r in bookings if r.owner_id == "org:a"
        ))
        assert released.status == "released"
        assert released.stopped_at == _ORIGIN + timedelta(seconds=0.2)
        assert ledger.get(next(
            r.reservation_id for r in bookings if r.owner_id == "org:b"
        )).status == "reserved"
        assert ledger.audit()


def test_pg_contested_native_shared_unit_rolls_back_source_and_peer_progresses(
    monkeypatch,
):
    """Two distinct PG connections speculatively acquire SimPy then race to COMMIT.

    Both native witnesses are deliberately held at the same pre-commit barrier.
    Only the pool-scoped *PostgreSQL* arbitration can decide a winner; removing
    it must cause double booking or an otherwise visible causal inconsistency.
    """
    assert DSN
    ns = _namespace()
    occupied = _binding("unit-1")
    independent = _binding("unit-2")
    racers = ("org:racer-a", "org:racer-b")
    with PostgresPersistence(DSN, namespace=ns) as seed:
        for org in racers:
            _seed(seed, org, occupied)
        _seed(seed, "org:peer", independent)

    barrier = Barrier(2)
    original_prepare = od5a_recurring._RecurringNativePhysicalArbiter.prepare

    def two_real_grants_before_commit(self, *, releases, grants, at):
        original_prepare(self, releases=releases, grants=grants, at=at)
        if self.execution_pool in {
            f"{org}/execution-pool" for org in racers
        }:
            assert grants, "contenders never reached native SimPy acquisition"
            # This synchronization is OUTSIDE any PostgreSQL UoW or pool lock.
            # Both worker connections must reach the pool arbiter concurrently.
            barrier.wait(timeout=30)

    monkeypatch.setattr(
        od5a_recurring._RecurringNativePhysicalArbiter,
        "prepare", two_real_grants_before_commit,
    )

    def worker(org):
        with PostgresPersistence(DSN, namespace=ns) as store:
            try:
                assert _tick(store, org, occupied).action == "advanced"
                return (org, "granted")
            except ResourceCapacityError:
                return (org, "capacity-wait")

    with ThreadPoolExecutor(max_workers=2) as workers:
        future_a = workers.submit(worker, racers[0])
        future_b = workers.submit(worker, racers[1])
        outcomes = dict((
            future_a.result(timeout=60),
            future_b.result(timeout=60),
        ))

    assert sorted(outcomes.values()) == ["capacity-wait", "granted"], outcomes
    winner = next(org for org in racers if outcomes[org] == "granted")
    loser = next(org for org in racers if outcomes[org] == "capacity-wait")
    with PostgresPersistence(DSN, namespace=ns) as check:
        ledger = check.temporal_resources()
        bookings = ledger.reservations()
        assert len(bookings) == 1 and bookings[0].owner_id == winner
        assert bookings[0].address.instance_id == "unit-1"
        assert _source(check, winner)["step"] == 1
        assert _source(check, loser)["step"] == 0
        assert check.job_state(winner).run_count == 1
        assert check.job_state(loser).run_count == 0
        assert check.job_state(loser).last_completed_trigger_id is None
        assert not any(
            r.resource_name.startswith(f"{loser}/")
            for r in check.resource_reservations()
        )
        assert not any(e.owner_id == loser for e in check.events())
        assert ledger.audit()
        # A third independent owner must still progress, even though the
        # winner retains this instance's durable reservation.
        assert _tick(check, "org:peer", independent).action == "advanced"
        assert _source(check, "org:peer")["step"] == 1
        assert _source(check, winner)["step"] == 1
        assert len(ledger.reservations()) == 2

    with PostgresPersistence(DSN, namespace=ns) as restarted:
        assert {r.owner_id for r in restarted.temporal_resources().reservations()} == {
            winner, "org:peer",
        }
        assert _source(restarted, loser)["step"] == 0
        assert restarted.temporal_resources().audit()


def test_pg_stale_physical_booking_is_rejected_before_source_commit():
    assert DSN
    ns = _namespace()
    binding = _binding("unit-1")
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, "org:a", binding)
        assert _tick(store, "org:a", binding).action == "advanced"
        local = next(r for r in store.resource_reservations()
                     if r.resource_name == "org:a/execution:0")
        booking_id = _external_id(local.reservation_id, "org:a")
        # Simulate an independent authoritative physical failure/release.
        store.temporal_resources().release(
            booking_id, at=_ORIGIN + timedelta(seconds=0.1),
        )
        physical_before = store.temporal_resources().snapshot()
        source_before = _source(store, "org:a")
        with pytest.raises(ResourceConflictError, match="matching fenced"):
            _tick(store, "org:a", binding)
        assert _source(store, "org:a") == source_before
        assert store.temporal_resources().snapshot() == physical_before
        assert store.temporal_resources().audit()


def test_pg_explicit_binding_is_required_and_cannot_change_on_replay():
    assert DSN
    ns = _namespace()
    binding = _binding("unit-1")
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, "org:a", binding)
        with pytest.raises(ValueError, match="explicit immutable binding"):
            run_od5a_recurring_tick(
                store, job_id="org:a", owner_id="worker:a",
                now=NOW, lease_duration=timedelta(seconds=60),
                trigger_id=deterministic_id("od5a-recurring-tick", "org:a", 1),
            )
        with pytest.raises(ValueError, match="differs"):
            _tick(store, "org:a", _binding("unit-2"))
        assert _source(store, "org:a")["step"] == 0
        assert store.temporal_resources().reservations() == ()


def test_pg_authority_rejects_unowned_physical_address_and_invalid_causes():
    assert DSN
    ns = _namespace()
    binding = _binding("unit-1")
    with pytest.raises(TypeError, match="real PostgreSQL"):
        SharedPhysicalGrantAuthority(MemoryPersistence(), binding, "org:a")
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, "org:a", binding)
        authority = SharedPhysicalGrantAuthority(store, binding, "org:a")
        assert authority._slot("org:a/execution:0") == ("execution:0", "unit-1")
        assert authority._slot("org:a/coordination:0") is None
        with pytest.raises(ResourceConflictError, match="another source owner"):
            authority._slot("org:b/execution:0")
        assert authority._id("local-lease") == _external_id("local-lease", "org:a")

        local = ResourceReservation(
            reservation_id="untrusted", request_id="untrusted-request",
            resource_name="org:a/execution:0", acquired_at=_ORIGIN,
            sequence=0,
        )
        with store.transaction() as uow:
            with authority.guarded_pool():
                with pytest.raises(ResourceConflictError, match="missing active source work"):
                    authority.apply(
                        before=(), releases=(), grants=(local,),
                        after_state={"active": {}, "actions": []}, at=0.0, uow=uow,
                    )
                with pytest.raises(ResourceConflictError, match="missing source causal parents"):
                    authority.apply(
                        before=(), releases=(), grants=(local,),
                        after_state={
                            "active": {"execution:0": {
                                "reservation_id": "untrusted", "causes": ["missing:event"],
                            }},
                            "actions": [{"kind": "execution_done",
                                         "resource": "execution:0", "at": 0.2}],
                        }, at=0.0, uow=uow,
                    )
        assert store.temporal_resources().reservations() == ()
