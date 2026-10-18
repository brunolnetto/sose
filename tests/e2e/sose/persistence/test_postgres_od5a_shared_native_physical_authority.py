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
    ResourceConflictError, TemporalOutage, authoritative_effect_reservation_id,
)
from sose.core.runtime import ResourceReservation
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational import od5a_recurring
from sose.organizational.od5a_recurring import (
    SharedPhysicalGrantAuthority, SharedPhysicalPoolBinding,
    initialize_od5a_recurring_job, run_od5a_recurring_tick,
    suggest_od5a_shared_wait_slots,
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
            outcome = _tick(store, org, occupied)
            assert outcome.action in {"advanced", "waiting"}
            return (org, "granted" if outcome.action == "advanced" else "capacity-wait")

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
        assert check.job_state(loser).active_trigger_id == deterministic_id(
            "od5a-recurring-tick", loser, 1,
        )
        wait_id = deterministic_id(
            "od5a-shared-capacity-wait", loser,
            check.job_state(loser).active_trigger_id,
        )
        wait = check.entity("od5a_shared_capacity_wait", wait_id)
        assert wait is not None and wait.state == "waiting"
        assert wait.attributes["binding_fingerprint"] == occupied.fingerprint()
        assert len(wait.attributes["requests"]) == 1
        assert wait.attributes["requests"][0]["instance_id"] == "unit-1"
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


def test_pg_shared_physical_outage_waits_without_retroactive_allocation():
    """Recovery AFTER source due must not erase already unavailable physical time.

    A physical interval cannot be occupied at 0.0 seconds merely because its
    outage was later cleared at 0.1 seconds. A future admissible reschedule
    (separate #477 contract) is required before this wait can be satisfied.
    """
    assert DSN
    ns = _namespace()
    binding, independent = _binding("unit-1"), _binding("unit-2")
    org, peer = "org:outage", "org:independent"
    trigger = deterministic_id("od5a-recurring-tick", org, 1)
    wait_id = deterministic_id("od5a-shared-capacity-wait", org, trigger)
    outage = TemporalOutage(
        outage_id="outage:unit-1", address=binding.pool.address(instance_id="unit-1"),
        start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
    )
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        _seed(store, peer, independent)
        store.temporal_resources().fail(outage)
        before_source = _source(store, org)
        before_events = tuple(e for e in store.events() if e.owner_id == org)
        assert before_events == ()
        first = _tick(store, org, binding)
        assert first.action == "waiting"
        assert first.trigger_id == trigger and first.logical_tick == 0
        assert _source(store, org) == before_source
        assert store.temporal_resources().reservations() == ()
        assert tuple(e for e in store.events() if e.owner_id == org) == before_events
        wait = store.entity("od5a_shared_capacity_wait", wait_id)
        assert wait is not None and wait.state == "waiting"
        assert wait.attributes["trigger_id"] == trigger
        assert wait.attributes["source_action"] == before_source["actions"][0]
        assert wait.attributes["requests"][0]["instance_id"] == "unit-1"
        assert wait.attributes["origin_epoch"] == wait.attributes["last_claim_epoch"] == 1
        # Repeated blocked triggers do not duplicate waits or physical writes.
        assert _tick(store, org, binding).action == "waiting"
        assert store.entity("od5a_shared_capacity_wait", wait_id) == wait
        assert _source(store, org) == before_source
        assert store.job_state(org).active_trigger_id == trigger
        assert _tick(store, peer, independent).action == "advanced"
        assert _source(store, peer)["step"] == 1
        assert len(store.temporal_resources().reservations()) == 1

    with PostgresPersistence(DSN, namespace=ns) as restarted:
        assert restarted.entity("od5a_shared_capacity_wait", wait_id).state == "waiting"
        # Recover after 0.1 seconds: historical [0,0.1) outage must remain
        # effective. Requested source service starts at 0.0: no fake grant.
        restarted.temporal_resources().recover(
            outage.outage_id, at=_ORIGIN + timedelta(seconds=0.1),
        )
        still_waiting = _tick(restarted, org, binding)
        assert still_waiting.action == "waiting"
        assert still_waiting.trigger_id == trigger and still_waiting.logical_tick == 0
        assert restarted.entity("od5a_shared_capacity_wait", wait_id) == wait
        assert _source(restarted, org) == before_source
        assert restarted.job_state(org).last_completed_trigger_id is None
        assert restarted.job_state(org).active_trigger_id == trigger
        assert not any(e.owner_id == org for e in restarted.events())
        assert {r.owner_id for r in restarted.temporal_resources().reservations()} == {peer}
        assert restarted.temporal_resources().audit()


@pytest.mark.parametrize("corruption", ("extra", "bool_seq", "bool_at"))
def test_pg_corrupted_wait_payload_cannot_be_replayed_as_immutable_authority(corruption):
    """A forged extra durable attribute is not a valid wait/runner history."""
    assert DSN
    ns = _namespace()
    org, binding = "org:tamper", _binding("unit-1")
    trigger = deterministic_id("od5a-recurring-tick", org, 1)
    wait_id = deterministic_id("od5a-shared-capacity-wait", org, trigger)
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        store.temporal_resources().fail(TemporalOutage(
            outage_id="outage:tamper", address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        ))
        assert _tick(store, org, binding).action == "waiting"
        state_before = _source(store, org)
        with store.transaction() as uow:
            wait = uow.get_entity("od5a_shared_capacity_wait", wait_id)
            assert wait is not None
            if corruption == "extra":
                wait.attributes["untrusted_extra_claim"] = "not-an-authority"
            elif corruption == "bool_seq":
                assert wait.attributes["source_action"]["seq"] == 0
                wait.attributes["source_action"]["seq"] = False
            else:
                assert wait.attributes["source_action"]["at"] == 0.0
                wait.attributes["source_action"]["at"] = False
            if corruption != "extra":
                # MemoryUnitOfWork's dirty-equality legitimately considers
                # False == 0/0.0; force an actual PostgreSQL mutation first.
                # A SECOND committed transaction removes the staging marker
                # while preserving the typed bool payload in SQL.
                wait.attributes["staged_sql_mutation"] = "written"
            uow.save_entity(wait)
        if corruption != "extra":
            with store.transaction() as uow:
                wait = uow.get_entity("od5a_shared_capacity_wait", wait_id)
                assert wait is not None
                field = "seq" if corruption == "bool_seq" else "at"
                assert wait.attributes["source_action"][field] is False
                assert wait.attributes.pop("staged_sql_mutation") == "written"
                uow.save_entity(wait)
        with PostgresPersistence(DSN, namespace=ns) as independent:
            durable = independent.entity("od5a_shared_capacity_wait", wait_id)
            assert durable is not None
            if corruption != "extra":
                assert durable.attributes["source_action"][field] is False
                assert "staged_sql_mutation" not in durable.attributes
            else:
                assert durable.attributes["untrusted_extra_claim"] == "not-an-authority"
        with pytest.raises(ResourceConflictError, match="wait record contradicts"):
            _tick(store, org, binding)
        assert _source(store, org) == state_before
        assert store.job_state(org).active_trigger_id == trigger
        assert store.temporal_resources().reservations() == ()
        assert not any(e.owner_id == org for e in store.events())
        assert store.temporal_resources().audit()

def test_pg_shared_wait_advice_is_prospective_and_preserves_all_truth():
    """A recovered outage can suggest a NEW slot, never an old physical grant."""
    assert DSN
    ns = _namespace()
    org, binding = "org:advice", _binding("unit-1")
    start = _ORIGIN + timedelta(milliseconds=100)
    wait_id = deterministic_id(
        "od5a-shared-capacity-wait", org,
        deterministic_id("od5a-recurring-tick", org, 1),
    )
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        outage = TemporalOutage(
            outage_id="advice:down",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        )
        ledger = store.temporal_resources()
        ledger.fail(outage)
        assert _tick(store, org, binding).action == "waiting"
        ledger.recover(outage.outage_id, at=start)
        snapshot = ledger.snapshot()
        source = _source(store, org)
        job = store.job_state(org)
        wait = store.entity("od5a_shared_capacity_wait", wait_id)
        assert wait is not None and wait.state == "waiting"

        too_early = suggest_od5a_shared_wait_slots(
            store, job_id=org, shared_pool_binding=binding,
            not_before=_ORIGIN, latest_start=_ORIGIN,
        )
        assert len(too_early) == 1 and too_early[0].suggested_start is None
        advised = suggest_od5a_shared_wait_slots(
            store, job_id=org, shared_pool_binding=binding,
            not_before=start, latest_start=start,
        )
        assert len(advised) == 1
        assert advised[0].reservation_id == wait.attributes["requests"][0]["reservation_id"]
        assert advised[0].instance_id == "unit-1"
        assert advised[0].original_start == _ORIGIN
        assert advised[0].duration == timedelta(seconds=0.2)
        assert advised[0].suggested_start == start
        assert ledger.snapshot() == snapshot
        assert _source(store, org) == source
        assert store.job_state(org) == job
        assert store.entity("od5a_shared_capacity_wait", wait_id) == wait
        assert not any(e.owner_id == org for e in store.events())
        assert ledger.get(advised[0].reservation_id) is None
        assert ledger.audit()
    with PostgresPersistence(DSN, namespace=ns) as reopened:
        assert suggest_od5a_shared_wait_slots(
            reopened, job_id=org, shared_pool_binding=binding,
            not_before=start, latest_start=start,
        ) == advised
        assert reopened.temporal_resources().audit()


def test_pg_wait_advice_is_stale_after_peer_reserves_suggested_slot():
    assert DSN
    ns = _namespace()
    org, binding = "org:advice-stale", _binding("unit-1")
    start = _ORIGIN + timedelta(milliseconds=100)
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        ledger = store.temporal_resources()
        outage = TemporalOutage(
            outage_id="advice:stale", address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        )
        ledger.fail(outage)
        assert _tick(store, org, binding).action == "waiting"
        ledger.recover(outage.outage_id, at=start)
        advice = suggest_od5a_shared_wait_slots(
            store, job_id=org, shared_pool_binding=binding,
            not_before=start, latest_start=start + timedelta(seconds=1),
        )
        assert advice[0].suggested_start == start

    with PostgresPersistence(DSN, namespace=ns) as contender:
        from sose.core.resource_reservations import TemporalReservation
        contender.temporal_resources().reserve(TemporalReservation(
            reservation_id="another-owner", owner_id="org:contender",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=start, end_at=start + timedelta(seconds=0.2),
            causation_id="independent:physical-event",
        ))
    with PostgresPersistence(DSN, namespace=ns) as reopened:
        next_advice = suggest_od5a_shared_wait_slots(
            reopened, job_id=org, shared_pool_binding=binding,
            not_before=start, latest_start=start + timedelta(seconds=1),
        )
        assert next_advice[0].suggested_start == start + timedelta(seconds=0.2)
        assert reopened.temporal_resources().audit()


def test_pg_wait_advice_rejects_corrupted_causal_wait_record():
    assert DSN
    ns = _namespace()
    org, binding = "org:advice-forged", _binding("unit-1")
    trigger = deterministic_id("od5a-recurring-tick", org, 1)
    wait_id = deterministic_id("od5a-shared-capacity-wait", org, trigger)
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        store.temporal_resources().fail(TemporalOutage(
            outage_id="advice:forged",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        ))
        assert _tick(store, org, binding).action == "waiting"
        with store.transaction() as uow:
            wait = uow.get_entity("od5a_shared_capacity_wait", wait_id)
            wait.attributes["requests"][0]["instance_id"] = "unit-2"
            uow.save_entity(wait)
        source = _source(store, org)
        with pytest.raises(ResourceConflictError, match="wait record contradicts"):
            suggest_od5a_shared_wait_slots(
                store, job_id=org, shared_pool_binding=binding,
                not_before=_ORIGIN, latest_start=_ORIGIN + timedelta(seconds=2),
            )
        assert _source(store, org) == source
        assert store.temporal_resources().reservations() == ()
        assert store.temporal_resources().audit()

@pytest.mark.parametrize("forgery", ("service", "causal_parent"))
def test_pg_wait_advice_rejects_mutated_source_after_durable_wait(forgery):
    """Wait authentication must bind service duration AND causal provenance."""
    assert DSN
    ns = _namespace()
    org, binding = "org:source-forged", _binding("unit-1")
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        store.temporal_resources().fail(TemporalOutage(
            outage_id="source:down",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        ))
        assert _tick(store, org, binding).action == "waiting"
        with store.transaction() as uow:
            source = uow.get_entity("od5a_durable_pilot", org)
            assert source is not None
            if forgery == "service":
                item = next(iter(source.attributes["services"]))
                stage = next(iter(source.attributes["services"][item]))
                source.attributes["services"][item][stage] = 0.9
            else:
                # Initial arrival action's item determines the root stage's
                # deterministic causal event; alter it without changing the
                # original action identity persisted in the wait.
                source.attributes["services"]["forged:item"] = {
                    "a": 0.2, "b": 0.2, "c": 0.1,
                }
                # This changes the source's causal contract even though
                # previewed local resource IDs can remain the same.
            uow.save_entity(source)
        with pytest.raises(ResourceConflictError, match="wait record contradicts"):
            suggest_od5a_shared_wait_slots(
                store, job_id=org, shared_pool_binding=binding,
                not_before=_ORIGIN, latest_start=_ORIGIN + timedelta(seconds=2),
            )
        assert store.temporal_resources().reservations() == ()
        assert store.temporal_resources().audit()


@pytest.mark.parametrize("forgery", ("empty_owner", "bool_epoch", "wrong_trigger"))
def test_pg_wait_advice_validates_full_durable_claim_history(forgery):
    assert DSN
    ns = _namespace()
    org, binding = "org:claim-forged", _binding("unit-1")
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        store.temporal_resources().fail(TemporalOutage(
            outage_id="claim:down",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        ))
        assert _tick(store, org, binding).action == "waiting"
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", org)
            assert lease is not None
            if forgery == "empty_owner":
                lease.attributes["owner_id"] = ""
            elif forgery == "bool_epoch":
                lease.attributes["epoch"] = False
                lease.attributes["staged_sql_mutation"] = True
            else:
                lease.attributes["trigger_id"] = "forged-trigger"
            uow.save_entity(lease)
        with pytest.raises((ResourceConflictError, ValueError), match="lease|claim"):
            suggest_od5a_shared_wait_slots(
                store, job_id=org, shared_pool_binding=binding,
                not_before=_ORIGIN, latest_start=_ORIGIN + timedelta(seconds=2),
            )
        assert store.temporal_resources().reservations() == ()


def test_pg_wait_advice_does_not_bootstrap_namespace_or_mutate_ledger(monkeypatch):
    assert DSN
    from sose.persistence.postgres_resource_ledger import PostgresTemporalResourceLedger
    ns = _namespace()
    org, binding = "org:readonly", _binding("unit-1")
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        ledger = store.temporal_resources()
        ledger.fail(TemporalOutage(
            outage_id="readonly:down",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        ))
        assert _tick(store, org, binding).action == "waiting"
        snapshot = ledger.snapshot()

        def reject_bootstrap(self):
            raise AssertionError("read-only advice must not run bootstrap DDL")

        monkeypatch.setattr(PostgresTemporalResourceLedger, "_bootstrap", reject_bootstrap)
        advice = suggest_od5a_shared_wait_slots(
            store, job_id=org, shared_pool_binding=binding,
            not_before=_ORIGIN, latest_start=_ORIGIN + timedelta(seconds=2),
        )
        assert advice[0].suggested_start == _ORIGIN + timedelta(seconds=1)
        assert ledger.snapshot() == snapshot
        assert ledger.audit()


@pytest.mark.parametrize("missing", ("wait_schema_version", "source_digest"))
def test_pg_downgraded_wait_never_authenticates_retry_or_read_advice(missing):
    """Deleting a v2 field must not turn it into a trusted legacy waiting claim."""
    assert DSN
    ns = _namespace()
    org, binding = "org:downgrade", _binding("unit-1")
    trigger = deterministic_id("od5a-recurring-tick", org, 1)
    wait_id = deterministic_id("od5a-shared-capacity-wait", org, trigger)
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        store.temporal_resources().fail(TemporalOutage(
            outage_id="downgrade:outage",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        ))
        assert _tick(store, org, binding).action == "waiting"
        before = _source(store, org)
        with store.transaction() as uow:
            wait = uow.get_entity("od5a_shared_capacity_wait", wait_id)
            assert wait is not None
            assert wait.attributes.pop(missing)
            uow.save_entity(wait)
        with pytest.raises(ResourceConflictError, match="wait record contradicts"):
            suggest_od5a_shared_wait_slots(
                store, job_id=org, shared_pool_binding=binding,
                not_before=_ORIGIN, latest_start=_ORIGIN + timedelta(seconds=2),
            )
        with pytest.raises(ResourceConflictError, match="wait record contradicts"):
            _tick(store, org, binding)
        assert _source(store, org) == before
        assert store.job_state(org).active_trigger_id == trigger
        assert store.temporal_resources().reservations() == ()
        assert store.temporal_resources().audit()


def test_pg_wait_with_v1_schema_never_acquires_physical_capacity():
    """Unverifiable historical wait identity is not an execution grant."""
    assert DSN
    ns = _namespace()
    org, binding = "org:pre-v2", _binding("unit-1")
    trigger = deterministic_id("od5a-recurring-tick", org, 1)
    wait_id = deterministic_id("od5a-shared-capacity-wait", org, trigger)
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        store.temporal_resources().fail(TemporalOutage(
            outage_id="pre-v2:down",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        ))
        assert _tick(store, org, binding).action == "waiting"
        with store.transaction() as uow:
            wait = uow.get_entity("od5a_shared_capacity_wait", wait_id)
            assert wait is not None
            wait.attributes.pop("source_digest")
            wait.attributes.pop("wait_schema_version")
            uow.save_entity(wait)
        store.temporal_resources().recover(
            "pre-v2:down", at=_ORIGIN,
        )
        before = _source(store, org)
        with pytest.raises(ResourceConflictError, match="wait record contradicts"):
            _tick(store, org, binding)
        assert _source(store, org) == before
        assert store.temporal_resources().reservations() == ()
        assert store.temporal_resources().audit()


def test_pg_missing_wait_entity_cannot_launder_a_blocked_claim_into_a_grant():
    """Durable runner marker survives independent deletion of the wait record."""
    assert DSN
    from psycopg import sql
    ns = _namespace()
    org, binding = "org:lost-wait", _binding("unit-1")
    trigger = deterministic_id("od5a-recurring-tick", org, 1)
    wait_id = deterministic_id("od5a-shared-capacity-wait", org, trigger)
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        ledger = store.temporal_resources()
        ledger.fail(TemporalOutage(
            outage_id="lost-wait:outage",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        ))
        assert _tick(store, org, binding).action == "waiting"
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", org)
            assert lease.attributes["physical_wait_id"] == wait_id

        # Fault inject direct deletion of exactly this durable wait, with
        # the independent claimed-lease marker intentionally left intact.
        with store._connection.transaction():
            deleted = store._connection.execute(sql.SQL("""
                DELETE FROM {} WHERE collection = 'entities'
                AND record_key LIKE %s
            """).format(store._record_table), (f"%{wait_id}%",))
            assert deleted.rowcount == 1
        ledger.recover("lost-wait:outage", at=_ORIGIN)

    with PostgresPersistence(DSN, namespace=ns) as reopened:
        assert reopened.entity("od5a_shared_capacity_wait", wait_id) is None
        assert reopened.entity("od5a_runner_lease", org).attributes["physical_wait_id"] == wait_id
        source_before = _source(reopened, org)
        with pytest.raises(ResourceConflictError, match="pending physical wait|missing shared wait"):
            _tick(reopened, org, binding)
        assert _source(reopened, org) == source_before
        assert reopened.temporal_resources().reservations() == ()
        assert reopened.job_state(org).active_trigger_id == trigger
        assert reopened.temporal_resources().audit()


def test_pg_missing_wait_marker_cannot_be_ignored_by_advice_or_execution():
    assert DSN
    ns = _namespace()
    org, binding = "org:lost-marker", _binding("unit-1")
    trigger = deterministic_id("od5a-recurring-tick", org, 1)
    wait_id = deterministic_id("od5a-shared-capacity-wait", org, trigger)
    with PostgresPersistence(DSN, namespace=ns) as store:
        _seed(store, org, binding)
        ledger = store.temporal_resources()
        ledger.fail(TemporalOutage(
            outage_id="lost-marker:outage",
            address=binding.pool.address(instance_id="unit-1"),
            start_at=_ORIGIN, end_at=_ORIGIN + timedelta(seconds=1),
        ))
        assert _tick(store, org, binding).action == "waiting"
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", org)
            assert lease.attributes.pop("physical_wait_id") == wait_id
            uow.save_entity(lease)
        ledger.recover("lost-marker:outage", at=_ORIGIN)
        with pytest.raises(ResourceConflictError, match="pending physical wait|missing shared wait"):
            suggest_od5a_shared_wait_slots(
                store, job_id=org, shared_pool_binding=binding,
                not_before=_ORIGIN, latest_start=_ORIGIN + timedelta(seconds=1),
            )
        with pytest.raises(ResourceConflictError, match="pending physical wait|missing shared wait"):
            _tick(store, org, binding)
        assert ledger.reservations() == ()
        assert ledger.audit()
