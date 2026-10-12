"""OD-5a restartable physical-boundary orchestration; OS-kill falsifiers.

The read-side reconciler is NOT the actual SimPy production scheduler.
It may only publish committed physical events, and advances one durable
publication/claim/ACK/Command/observation/certificate phase per call.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from multiprocessing import get_context

import pytest

from sose.composition.boundary import (
    BoundaryLease, StaleBoundaryClaimError,
)
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_durable import (
    advance_od5a_durable, initialize_od5a_durable, od5a_durable_events,
)
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_reconciler import reconcile_od5a_boundary_step
from sose.organizational.od5a_boundary import consume_od5a_physical_completion
from sose.persistence.sqlite import SQLitePersistence


NOW = datetime(2010, 1, 1, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)
PHASES = ("published", "claimed", "acknowledged", "materialized", "certified")


def _arms():
    model = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8, "execution_slots": 1,
            "coordination_latency": 0.1, "service_cv": 0.5,
        },
        parameter_evidence={
            name: EvidenceClass.ASSUMED for name in (
                "arrival_rate", "execution_slots",
                "coordination_latency", "service_cv",
            )
        },
    )
    return {arm.arm_id: arm for arm in compile_od5a_arms(
        baseline=model, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            name: OD5aChargedCost(8, 1, 1, 1)
            for name in ("capacity_only", "topology_only",
                         "coordination_only", "structural_combined")
        },
    )}


def _seed(path, jobs):
    with SQLitePersistence(path) as store:
        for job_id, arm_name in jobs.items():
            initialize_od5a_durable(
                store, job_id=job_id, arm=_arms()[arm_name],
                items=(OD5aWorkItem(
                    item_id="item:" + job_id, arrival_at=0.0,
                    service_by_station=(("a", 0.2), ("b", 0.3), ("c", 0.1)),
                ),),
            )
        for job_id in jobs:
            for _ in range(100):
                if not advance_od5a_durable(store, job_id=job_id):
                    break
            else:
                raise AssertionError("source job never completed its physical DAG")
            assert any(e.payload["kind"] == "execution"
                       for e in od5a_durable_events(store, job_id=job_id))
        return tuple(store.events())


def _step(path, job="org:alpha", owner="worker-A", now=NOW):
    with SQLitePersistence(path) as store:
        return reconcile_od5a_boundary_step(
            store, job_id=job, owner_id=owner,
            now=now, lease_duration=LEASE,
        )


def _drain(path, job="org:alpha", owner="worker-A", now=NOW):
    actions = []
    for _ in range(250):
        result = _step(path, job=job, owner=owner, now=now)
        actions.append(result.action)
        if result.action in {"complete", "idle", "blocked"}:
            return actions
    raise AssertionError("reconciler failed to reach bounded stable state")


def _snapshot(path, jobs):
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            deliveries = tuple(sorted(
                uow.boundary_deliveries(), key=lambda d: d.delivery_id,
            ))
            messages = tuple(
                uow.get_boundary_message(d.message_id) for d in deliveries
            )
            observations = tuple(sorted(
                (uow.get_entity("od5a_physical_observation", e.event_id)
                 for e in store.events()
                 if e.correlation_id in jobs
                 and e.name in {"od5a.execution.v1", "od5a.coordination.v1"}),
                key=lambda x: x.id if x else "",
            ))
            return (
                tuple(sorted(store.events(), key=lambda x: x.event_id)),
                tuple(uow.get_entity("od5a_durable_pilot", j) for j in sorted(jobs)),
                tuple(sorted(store.resource_reservations(),
                             key=lambda x: x.reservation_id)),
                tuple(sorted(store.resource_definitions(),
                             key=lambda x: x.name)),
                messages,
                deliveries,
                tuple(uow.get_boundary_consumption(d.delivery_id) for d in deliveries),
                observations,
                tuple(uow.get_command(d.consumer_effect_id)
                      for d in deliveries if d.consumer_effect_id),
                tuple(sorted(store.business_effects(), key=lambda x: x.effect_id)),
            )


@pytest.mark.parametrize("arm", [
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
])
def test_all_arms_reconcile_every_committed_physical_event_exactly_once(tmp_path, arm):
    path = tmp_path / "world.db"
    source_events = _seed(path, {"org:alpha": arm})
    assert len(source_events) > 2
    actions = _drain(path)
    assert actions[-1] == "complete"
    physical_count = sum(
        e.name in {"od5a.execution.v1", "od5a.coordination.v1"}
        for e in source_events
    )
    for action in PHASES:
        assert actions.count(action) == physical_count
    with SQLitePersistence(path) as store:
        assert tuple(store.events()) == source_events  # no synthetic physical events
        assert len(store.business_effects()) == physical_count
        assert not store.resource_reservations()
        assert all(proof.entity_type == "od5a_physical_observation"
                   and proof.terminal_state == "observed"
                   for proof in store.business_effects())
    assert _drain(path) == ["complete"]
    assert _snapshot(path, {"org:alpha"}) == _snapshot(path, {"org:alpha"})


def _death_between_phases(path: str, phase: str, when: str):
    import os
    if when == "before":
        os._exit(92)
    result = _step(path)
    os._exit(93 if result.action == phase else 95)


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("when", ["before", "after"])
def test_real_os_kill_at_every_boundary_matches_uninterrupted_truth(tmp_path, phase, when):
    control, interrupted = tmp_path / "control.db", tmp_path / "killed.db"
    jobs = {"org:alpha": "baseline"}
    _seed(control, jobs)
    _seed(interrupted, jobs)
    assert _drain(control)[-1] == "complete"
    for expected in PHASES[:PHASES.index(phase)]:
        result = _step(interrupted)
        assert result.action == expected
    child = get_context("spawn").Process(
        target=_death_between_phases,
        args=(str(interrupted), phase, when),
    )
    child.start()
    child.join(timeout=30)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("OS worker deadlocked around durable boundary")
    assert child.exitcode == (92 if when == "before" else 93)
    assert _drain(interrupted)[-1] == "complete"
    assert _snapshot(interrupted, jobs) == _snapshot(control, jobs)


def test_orphaned_fenced_claim_waits_then_reclaims_without_duplicating_effect(tmp_path):
    path = tmp_path / "fencing.db"
    _seed(path, {"org:alpha": "baseline"})
    assert _step(path).action == "published"
    first = _step(path, owner="worker-A")
    assert first.action == "claimed"
    assert first.event_id is not None
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            delivery = next(iter(uow.boundary_deliveries()))
            stale = BoundaryLease(
                delivery_id=delivery.delivery_id,
                message_id=delivery.message_id,
                owner_id=delivery.claim_owner_id,
                epoch=delivery.claim_epoch,
                lease_expires_at=delivery.lease_expires_at,
            )
    blocked = _step(path, owner="worker-B")
    assert blocked.action == "blocked"
    reclaimed = _step(path, owner="worker-B", now=NOW + LEASE)
    assert reclaimed.action == "claimed"
    with SQLitePersistence(path) as store:
        with pytest.raises(StaleBoundaryClaimError):
            consume_od5a_physical_completion(
                store, lease=stale, now=NOW + LEASE,
            )
        assert store.business_effects() == ()
    assert _step(path, owner="worker-B", now=NOW + LEASE).action == "acknowledged"
    assert _drain(path, owner="worker-B", now=NOW + LEASE)[-1] == "complete"
    with SQLitePersistence(path) as store:
        assert len(store.business_effects()) == len([
            event for event in store.events()
            if event.name in {"od5a.execution.v1", "od5a.coordination.v1"}
        ])


def test_organizations_interleave_without_leaking_claims_or_physical_clocks(tmp_path):
    jobs = {"org:a": "baseline", "org:b": "capacity_only"}
    control, interleaved = tmp_path / "single.db", tmp_path / "interleaved.db"
    _seed(control, jobs)
    _seed(interleaved, jobs)
    for job in jobs:
        assert _drain(control, job=job, owner=job)[-1] == "complete"
    history = {j: [] for j in jobs}
    for _ in range(250):
        results = {
            job: _step(interleaved, job=job, owner=job)
            for job in jobs
        }
        for job, result in results.items():
            history[job].append(result.action)
        if all(result.action == "complete" for result in results.values()):
            break
    else:
        pytest.fail("interleaved organizations did not finish")
    assert all(PHASES[0] in events and PHASES[-1] in events
               for events in history.values())
    assert _snapshot(interleaved, jobs) == _snapshot(control, jobs)


def test_reconciler_never_advances_a_not_yet_committed_source_job(tmp_path):
    path = tmp_path / "running-source.db"
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(
            store, job_id="org:a", arm=_arms()["baseline"],
            items=(OD5aWorkItem(
                item_id="item:1", arrival_at=0.0,
                service_by_station=(("a", 0.2), ("b", 0.3), ("c", 0.1)),
            ),),
        )
        before = store.entity("od5a_durable_pilot", "org:a")
        assert reconcile_od5a_boundary_step(
            store, job_id="org:a", owner_id="worker",
            now=NOW, lease_duration=LEASE,
        ).action == "idle"
        assert store.entity("od5a_durable_pilot", "org:a") == before
        assert store.events() == ()



def test_no_false_complete_when_final_event_commits_before_authoritative_read(tmp_path):
    """Falsify stale candidate enumeration racing the final physical commit."""
    from sose.composition.model import DeliveryStatus

    reference = tmp_path / "source-reference.db"
    path = tmp_path / "source-race.db"
    expected_source = _seed(reference, {"org:alpha": "baseline"})
    physical_total = sum(
        e.name in {"od5a.execution.v1", "od5a.coordination.v1"}
        for e in expected_source
    )
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(
            store, job_id="org:alpha", arm=_arms()["baseline"],
            items=(OD5aWorkItem(
                item_id="item:org:alpha", arrival_at=0.0,
                service_by_station=(("a", 0.2), ("b", 0.3), ("c", 0.1)),
            ),),
        )
        for _ in range(100):
            current = od5a_durable_events(store, job_id="org:alpha")
            count = sum(e.name in {"od5a.execution.v1", "od5a.coordination.v1"}
                        for e in current)
            if count == physical_total - 1:
                break
            assert advance_od5a_durable(store, job_id="org:alpha")
        else:
            pytest.fail("could not position source before last physical event")
        assert not store.entity("od5a_durable_pilot", "org:alpha").attributes["done"]
    assert _drain(path)[-1] == "idle"

    with SQLitePersistence(path) as reader:
        transaction = reader.transaction
        invoked = False

        def inject_one_real_source_commit():
            nonlocal invoked
            if not invoked:
                invoked = True
                with SQLitePersistence(path) as writer:
                    assert advance_od5a_durable(writer, job_id="org:alpha")
                    assert writer.entity(
                        "od5a_durable_pilot", "org:alpha",
                    ).attributes["done"]
            return transaction()

        reader.transaction = inject_one_real_source_commit
        result = reconcile_od5a_boundary_step(
            reader, job_id="org:alpha", owner_id="worker-A",
            now=NOW, lease_duration=LEASE,
        )
        assert invoked
        assert result.action == "published", (
            "a current done state must never certify a stale physical-event index"
        )
        assert result.event_id not in {
            e.event_id for e in expected_source
            if e.name == "od5a.arrival.v1"
        }
        with transaction() as uow:
            delivery = next(
                d for d in uow.boundary_deliveries()
                if d.message_id == result.boundary_message_id
            )
            assert delivery.status is DeliveryStatus.PENDING
    assert _drain(path)[-1] == "complete"
    with SQLitePersistence(path) as store:
        physical = [
            event for event in store.events()
            if event.name in {"od5a.execution.v1", "od5a.coordination.v1"}
        ]
        assert len(store.business_effects()) == len(physical)


def test_future_dated_physical_event_waits_without_persisting_invalid_claim(tmp_path):
    from sose.composition.model import DeliveryStatus

    path = tmp_path / "future-event.db"
    _seed(path, {"org:alpha": "baseline"})
    before_event = datetime(1999, 12, 31, tzinfo=timezone.utc)
    assert _step(path, now=before_event).action == "published"
    blocked = _step(path, now=before_event)
    assert blocked.action == "blocked" and blocked.retry_at is not None
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            delivery = next(iter(uow.boundary_deliveries()))
            assert delivery.status is DeliveryStatus.PENDING
            assert delivery.attempts == 0 and delivery.claim_epoch == 0
    assert _drain(path)[-1] == "complete"


def test_regressed_worker_clock_cannot_ack_before_its_actual_claim(tmp_path):
    from sose.composition.model import DeliveryStatus

    path = tmp_path / "regressed-time.db"
    _seed(path, {"org:alpha": "baseline"})
    assert _step(path).action == "published"
    assert _step(path).action == "claimed"
    rejected = _step(path, now=NOW - timedelta(seconds=1))
    assert rejected.action == "blocked"
    assert rejected.retry_at == NOW
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            delivery = next(iter(uow.boundary_deliveries()))
            assert delivery.status is DeliveryStatus.CLAIMED
            assert uow.get_boundary_consumption(delivery.delivery_id) is None
    assert _step(path).action == "acknowledged"
    assert _drain(path)[-1] == "complete"
