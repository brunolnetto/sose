"""OD-5a OS-process SQLite worker falsifiers; NOT distributed fencing certification."""
from __future__ import annotations

from multiprocessing import get_context
from pathlib import Path

import pytest

from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_durable import (
    advance_od5a_durable,
    initialize_od5a_durable,
    od5a_durable_events,
    od5a_durable_state,
)
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.persistence.sqlite import SQLitePersistence


def _arms():
    model = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"], ["a", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8,
            "execution_slots": 1,
            "coordination_latency": 2.0,
            "service_cv": 0.5,
        },
        parameter_evidence={
            name: EvidenceClass.ASSUMED for name in (
                "arrival_rate", "execution_slots", "coordination_latency", "service_cv",
            )
        },
    )
    prices = {
        name: OD5aChargedCost(8, 1, 1, 1)
        for name in (
            "capacity_only", "topology_only",
            "coordination_only", "structural_combined",
        )
    }
    return {arm.arm_id: arm for arm in compile_od5a_arms(
        baseline=model,
        expanded_capacity=2,
        removed_dependency_edges=(("a", "c"),),
        reduced_coordination_latency=1.0,
        declared_costs=prices,
    )}


def _items():
    return (
        OD5aWorkItem(
            item_id=item_id, arrival_at=0.0,
            service_by_station=(("a", 0.2), ("b", 0.3), ("c", 0.1)),
        )
        for item_id in ("item:A", "item:B", "item:C")
    )


def _init(path: Path, jobs: dict[str, str]) -> None:
    with SQLitePersistence(path) as store:
        arms = _arms()
        for job_id, arm in jobs.items():
            initialize_od5a_durable(
                store, job_id=job_id, arm=arms[arm], items=tuple(_items()),
            )


def _drain(store: SQLitePersistence, job_id: str) -> None:
    for _ in range(150):
        if not advance_od5a_durable(store, job_id=job_id):
            return
    raise RuntimeError("OD-5a worker exceeded bounded causal progress")


def _worker(path: str, job_id: str, worker_index: int, ready, first_round, reports) -> None:
    # Construct a distinct OS-process SQLite connection before the shared
    # barrier; no process is allowed to drain the world on its own.
    with SQLitePersistence(path) as store:
        ready.wait(timeout=30)
        advanced = advance_od5a_durable(store, job_id=job_id)
        # Before any worker can drain, every other worker must have completed
        # its own *committed* authoritative step, not merely started a process.
        first_round.wait(timeout=30)
        if not advanced:
            raise AssertionError("race participant observed an already completed world")
        _drain(store, job_id)
        reports.put((worker_index, int(advanced)))


def _snapshot(path: Path, jobs: tuple[str, ...]):
    with SQLitePersistence(path) as store:
        return {
            job: (
                od5a_durable_state(store, job_id=job),
                od5a_durable_events(store, job_id=job),
            ) for job in jobs
        }, store.resource_reservations()


def _spawn_and_join(path: Path, target_jobs: tuple[str, ...]) -> tuple[int, ...]:
    context = get_context("spawn")
    ready = context.Barrier(len(target_jobs))
    first_round = context.Barrier(len(target_jobs))
    reports = context.Queue()
    children = [
        context.Process(
            target=_worker,
            args=(str(path), job_id, index, ready, first_round, reports),
        )
        for index, job_id in enumerate(target_jobs)
    ]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(timeout=45)
            if child.is_alive():
                child.kill()
                child.join()
                pytest.fail("OD-5a worker hung during concurrent authoritative advancement")
            assert child.exitcode == 0, f"worker exited abnormally: {child.exitcode}"
        reports_by_index = dict(reports.get(timeout=5) for _ in children)
        assert len(reports_by_index) == len(children), "lost worker progress attestations"
        return tuple(reports_by_index[index] for index in range(len(children)))
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
                child.join()
        reports.close()
        reports.join_thread()


@pytest.mark.parametrize("arm", [
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
])
def test_two_real_processes_competing_for_same_job_preserve_exact_causal_truth(tmp_path, arm):
    jobs = {"shared:job": arm}
    control, concurrent = tmp_path / "control.db", tmp_path / "concurrent.db"
    _init(control, jobs)
    _init(concurrent, jobs)
    with SQLitePersistence(control) as store:
        _drain(store, "shared:job")
    contributions = _spawn_and_join(concurrent, ("shared:job", "shared:job"))
    assert contributions == (1, 1), "both workers must advance authoritative causal steps"
    expected, no_leases = _snapshot(control, ("shared:job",))
    actual, recovered_leases = _snapshot(concurrent, ("shared:job",))
    assert actual == expected
    assert not no_leases and not recovered_leases
    state, events = actual["shared:job"]
    assert state["done"]
    assert len({event.event_id for event in events}) == len(events)
    assert all(event.correlation_id == "shared:job" for event in events)


def test_independent_organizations_retain_local_clocks_and_causal_events(tmp_path):
    jobs = {
        "org:alpha": "baseline",
        "org:beta": "capacity_only",
        "org:gamma": "coordination_only",
    }
    control, concurrent = tmp_path / "single.db", tmp_path / "parallel.db"
    _init(control, jobs)
    _init(concurrent, jobs)
    with SQLitePersistence(control) as store:
        for job_id in jobs:
            _drain(store, job_id)
    contributions = _spawn_and_join(concurrent, tuple(jobs))
    assert contributions == (1, 1, 1), "all organizations must execute in real child processes"
    expected, leases = _snapshot(control, tuple(jobs))
    actual, current_leases = _snapshot(concurrent, tuple(jobs))
    assert actual == expected
    assert not leases and not current_leases
    for job_id, (state, events) in actual.items():
        assert state["done"] and not state["active"]
        assert set(state["completed"]) == {"item:A", "item:B", "item:C"}
        assert all(event.correlation_id == job_id for event in events)
        assert all(event.payload["model_spec_hash"] == state["model_spec_hash"]
                   for event in events)
