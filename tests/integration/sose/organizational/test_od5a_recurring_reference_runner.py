"""Bounded recurring *reference* runner, explicitly not production SimPy promotion."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from multiprocessing import get_context

import pytest

from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_durable import (
    initialize_od5a_durable, od5a_durable_events, od5a_durable_state,
)
from sose.organizational.od5a_mechanisms import OD5aWorkItem, run_od5a_pilot
from sose.organizational.od5a_reference_runner import advance_od5a_reference_occurrence
from sose.persistence.sqlite import SQLitePersistence

NOW = datetime(2010, 1, 1, tzinfo=timezone.utc)
LEASE = timedelta(seconds=30)


def _arms():
    baseline = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={"arrival_rate": 0.8, "execution_slots": 1,
                    "coordination_latency": 0.1, "service_cv": 0.5},
        parameter_evidence={k: EvidenceClass.ASSUMED for k in
                            ("arrival_rate", "execution_slots", "coordination_latency", "service_cv")},
    )
    return {arm.arm_id: arm for arm in compile_od5a_arms(
        baseline=baseline, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={k: OD5aChargedCost(8, 1, 1, 1)
                        for k in ("capacity_only", "topology_only", "coordination_only",
                                  "structural_combined")},
    )}


def _items(organization):
    return (OD5aWorkItem(
        item_id="item:" + organization, arrival_at=0.0,
        service_by_station=(("a", 0.2), ("b", 0.3), ("c", 0.1)),
    ),)


def _seed(path, jobs):
    with SQLitePersistence(path) as store:
        for job, arm in jobs.items():
            initialize_od5a_durable(
                store, job_id=job, arm=_arms()[arm], items=_items(job),
            )


def _advance(store, job, owner="owner"):
    return advance_od5a_reference_occurrence(
        store, job_id=job, owner_id=owner, now=NOW,
        lease_duration=LEASE,
    )


def _drain(path, jobs, *, reopen_each=False):
    actions = {job: [] for job in jobs if job != "__store__"}
    for _ in range(500):
        advanced = False
        for job in actions:
            if reopen_each:
                with SQLitePersistence(path) as store:
                    result = _advance(store, job)
            else:
                result = _advance(jobs["__store__"], job)
            actions[job].append(result.action)
            if result.action != "complete":
                advanced = True
        if not advanced:
            return actions
    pytest.fail("bounded reference recurrence did not converge")


def _truth(path, jobs):
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            return {
                job: (
                    od5a_durable_state(store, job_id=job),
                    tuple(od5a_durable_events(store, job_id=job)),
                    tuple(sorted(
                        (m for d in uow.boundary_deliveries()
                         if (m := uow.get_boundary_message(d.message_id)) is not None
                         and m.correlation_id == job), key=lambda x: x.message_id,
                    )),
                    tuple(sorted(
                        (d for d in uow.boundary_deliveries()
                         if (m := uow.get_boundary_message(d.message_id)) is not None
                         and m.correlation_id == job), key=lambda x: x.delivery_id,
                    )),
                    tuple(sorted(
                        (uow.get_boundary_consumption(d.delivery_id)
                         for d in uow.boundary_deliveries()
                         if (m := uow.get_boundary_message(d.message_id)) is not None
                         and m.correlation_id == job),
                        key=lambda x: x.delivery_id,
                    )),
                    tuple(sorted(
                        (uow.get_business_effect(d.consumer_effect_id)
                         for d in uow.boundary_deliveries()
                         if (m := uow.get_boundary_message(d.message_id)) is not None
                         and m.correlation_id == job),
                        key=lambda x: x.effect_id,
                    )),
                ) for job in jobs
            }, tuple(store.resource_reservations())


@pytest.mark.parametrize("arm", [
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
])
def test_reopened_reference_recurrence_equals_uninterrupted_and_simpy_trace(tmp_path, arm):
    jobs = {"org:a": arm}
    control, restarted = tmp_path / "control.db", tmp_path / "restart.db"
    _seed(control, jobs)
    _seed(restarted, jobs)
    with SQLitePersistence(control) as store:
        _drain(control, {**jobs, "__store__": store})
    steps = _drain(restarted, jobs, reopen_each=True)["org:a"]
    assert "source_advanced" in steps
    assert set(steps) >= {"published", "claimed", "acknowledged",
                          "materialized", "certified", "complete"}
    assert _truth(restarted, jobs) == _truth(control, jobs)
    state, events, *_ = _truth(control, jobs)[0]["org:a"]
    assert state["done"]
    expected = run_od5a_pilot(
        arm=_arms()[arm], items=_items("org:a"), horizon=100.0,
    )
    actual = sorted((
        e.payload["kind"], e.payload["item_id"], e.payload["source"],
        e.payload["target"], e.payload["start"], e.payload["end"],
        e.payload["resource"],
    ) for e in events if e.payload["kind"] != "arrival")
    assert actual == sorted((
        e.kind, e.item_id, e.source, e.target, e.start, e.end,
        e.resource_instance,
    ) for e in expected.events)


def _die_after_one_commit(path, job):
    import os
    with SQLitePersistence(path) as store:
        step = _advance(store, job)
        if step.action == "complete":
            raise RuntimeError("unexpected completed world")
        os._exit(87)


def test_worker_death_after_each_bounded_recurring_commit_replays_exactly(tmp_path):
    jobs = {"org:kill": "structural_combined"}
    killed, control = tmp_path / "killed.db", tmp_path / "control.db"
    _seed(killed, jobs)
    _seed(control, jobs)
    with SQLitePersistence(control) as store:
        _drain(control, {**jobs, "__store__": store})
    for _ in range(14):
        worker = get_context("spawn").Process(
            target=_die_after_one_commit, args=(str(killed), "org:kill"),
        )
        worker.start()
        worker.join(timeout=30)
        if worker.is_alive():
            worker.kill()
            worker.join()
            pytest.fail("recurring worker failed to terminate")
        assert worker.exitcode == 87
    _drain(killed, jobs, reopen_each=True)
    assert _truth(killed, jobs) == _truth(control, jobs)


def test_shared_db_keeps_each_organization_local_when_interleaved(tmp_path):
    jobs = {"org:alpha": "baseline", "org:beta": "capacity_only",
            "org:gamma": "coordination_only"}
    control, mixed = tmp_path / "control.db", tmp_path / "interleaved.db"
    _seed(control, jobs)
    _seed(mixed, jobs)
    with SQLitePersistence(control) as store:
        for job in jobs:
            _drain(control, {job: jobs[job], "__store__": store})
    _drain(mixed, jobs, reopen_each=True)
    assert _truth(mixed, jobs) == _truth(control, jobs)
    with SQLitePersistence(mixed) as store:
        assert store.resource_reservations() == ()


def test_unknown_organization_does_not_create_physical_world(tmp_path):
    with SQLitePersistence(tmp_path / "unknown.db") as store:
        with pytest.raises(KeyError):
            _advance(store, "org:missing")
        assert store.events() == ()
