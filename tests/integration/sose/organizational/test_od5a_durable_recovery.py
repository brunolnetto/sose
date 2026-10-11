"""OD-5a durable operational recovery falsification (not confirmatory DOE)."""
from __future__ import annotations

from multiprocessing import get_context
from pathlib import Path

import pytest

from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, OD5aCompiledArm, compile_od5a_arms
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_durable import (
    advance_od5a_durable, initialize_od5a_durable, od5a_durable_state,
    od5a_durable_events,
)
from sose.persistence.sqlite import SQLitePersistence


def _arms():
    model = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"], ["a", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8, "execution_slots": 1,
            "coordination_latency": 2.0, "service_cv": 0.5,
        },
        parameter_evidence={
            n: EvidenceClass.ASSUMED for n in (
                "arrival_rate", "execution_slots", "coordination_latency", "service_cv",
            )
        },
    )
    costs = {
        n: OD5aChargedCost(8, 1, 1, 1)
        for n in ("capacity_only", "topology_only", "coordination_only", "structural_combined")
    }
    return {a.arm_id: a for a in compile_od5a_arms(
        baseline=model, expanded_capacity=2,
        removed_dependency_edges=(("a", "c"),),
        reduced_coordination_latency=1.0, declared_costs=costs,
    )}



def _compiled_baseline_for(
    model: ModelSpec,
    *, removed: tuple[str, str],
    reduced_latency: float = 0.0,
) -> OD5aCompiledArm:
    """Custom falsification models must still pass the real five-arm compiler."""
    costs = {
        name: OD5aChargedCost(8, 1, 1, 1)
        for name in (
            "capacity_only", "topology_only", "coordination_only",
            "structural_combined",
        )
    }
    return compile_od5a_arms(
        baseline=model,
        expanded_capacity=model.parameters["execution_slots"] + 1,
        removed_dependency_edges=(removed,),
        reduced_coordination_latency=reduced_latency,
        declared_costs=costs,
    )[0]

def _items():
    return tuple(
        OD5aWorkItem(
            item_id=item_id, arrival_at=0.0,
            service_by_station=(("a", 1.0), ("b", 1.0), ("c", 1.0)),
        ) for item_id in ("x", "y", "z")
    )


def _finish(path: Path, arm_id: str, stop_after: int | None = None):
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(store, job_id="od5a-test", arm=_arms()[arm_id], items=_items())
        processed = 0
        while advance_od5a_durable(store, job_id="od5a-test"):
            processed += 1
            if stop_after is not None and processed == stop_after:
                break
            assert processed < 200, "durable work graph should terminate"
        state = od5a_durable_state(store, job_id="od5a-test")
        events = od5a_durable_events(store, job_id="od5a-test")
        resources = store.resource_reservations()
        return state, events, resources


@pytest.mark.parametrize("arm_id", [
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
])
def test_continuous_and_sqlite_reopen_equivalence_on_every_arm(tmp_path, arm_id):
    continuous_path = tmp_path / "continuous.db"
    restarted_path = tmp_path / "restarted.db"
    continuous = _finish(continuous_path, arm_id)
    _finish(restarted_path, arm_id, stop_after=4)
    _finish(restarted_path, arm_id, stop_after=5)
    recovered = _finish(restarted_path, arm_id)
    assert recovered == continuous
    state, events, reservations = recovered
    assert state["done"]
    assert not state["active"]
    assert not reservations
    assert set(state["completed"]) == {"x", "y", "z"}
    assert len({e.event_id for e in events}) == len(events)
    assert all(e.payload["model_spec_hash"] == _arms()[arm_id].model.model_spec_hash
               and e.payload["config_epoch"] == 0 for e in events)
    assert all(e.correlation_id == "od5a-test" for e in events)


def test_reopened_competing_worker_does_not_duplicate_committed_event(tmp_path):
    path = tmp_path / "same.db"
    _finish(path, "baseline", stop_after=1)
    with SQLitePersistence(path) as writer:
        first = advance_od5a_durable(writer, job_id="od5a-test")
    with SQLitePersistence(path) as reader:
        # Fresh processes derive the next action from authoritative state.
        second = advance_od5a_durable(reader, job_id="od5a-test")
    assert first and second
    assert _finish(path, "baseline") == _finish(tmp_path / "single.db", "baseline")


def _die_after_committed_event(path: str):
    """Real worker death after one durable atomic event and before response."""
    import os
    with SQLitePersistence(path) as store:
        # This exits the OS process after transaction COMMIT.
        advance_od5a_durable(store, job_id="od5a-test")
        os._exit(99)


def test_real_worker_death_after_committed_step_matches_continuous(tmp_path):
    path = tmp_path / "killed.db"
    _finish(path, "structural_combined", stop_after=2)
    child = get_context("spawn").Process(target=_die_after_committed_event, args=(str(path),))
    child.start()
    child.join(timeout=20)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("worker did not exit")
    assert child.exitcode == 99
    assert _finish(path, "structural_combined") == _finish(
        tmp_path / "continuous.db", "structural_combined"
    )


def test_conflicting_model_or_exogenous_workload_is_rejected_on_resume(tmp_path):
    path = tmp_path / "identity.db"
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(store, job_id="od5a-test", arm=_arms()["baseline"], items=_items())
        with pytest.raises(ValueError, match="immutable"):
            initialize_od5a_durable(
                store, job_id="od5a-test", arm=_arms()["topology_only"], items=_items(),
            )
        with pytest.raises(ValueError, match="immutable"):
            initialize_od5a_durable(
                store, job_id="od5a-test", arm=_arms()["baseline"], items=_items()[:1],
            )


@pytest.mark.parametrize("arm_id", [
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
])
def test_causal_predecessors_precede_effects_and_physical_intervals_never_overlap(tmp_path, arm_id):
    state, events, reservations = _finish(tmp_path / "causal.db", arm_id)
    already_committed: set[str] = set()
    by_resource: dict[str, list[tuple[float, float]]] = {}
    for event in events:
        assert set(event.payload["causes"]).issubset(already_committed)
        if event.causation_id:
            assert event.causation_id in already_committed
        assert event.payload["start"] <= event.payload["end"]
        resource = event.payload["resource"]
        if resource is not None:
            by_resource.setdefault(resource, []).append((
                event.payload["start"], event.payload["end"],
            ))
        already_committed.add(event.event_id)
    for intervals in by_resource.values():
        intervals.sort()
        assert all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:]))
    assert state["done"] and not reservations


def test_durable_identity_does_not_alias_colon_bearing_item_station_pairs(tmp_path):
    base = _arms()["baseline"]
    raw = base.model.canonical_payload()
    raw["stations"] = {"c": {}, "b:c": {}, "root": {}}
    raw["routing"] = {"dependency_edges": [["c", "root"], ["b:c", "root"]]}
    arm = _compiled_baseline_for(
        ModelSpec.model_validate(raw), removed=("c", "root"),
    )
    work = tuple(
        OD5aWorkItem(
            item_id=i, arrival_at=0.0,
            service_by_station=(("c", 1.0), ("b:c", 1.0), ("root", 1.0)),
        ) for i in ("a:b", "a")
    )
    path = tmp_path / "ambiguous_ids.db"
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(store, job_id="j:1", arm=arm, items=work)
        for _ in range(100):
            if not advance_od5a_durable(store, job_id="j:1"):
                break
        state = od5a_durable_state(store, job_id="j:1")
        events = od5a_durable_events(store, job_id="j:1")
        assert state["done"]
        assert set(state["completed"]) == {"a:b", "a"}
        assert len({e.event_id for e in events}) == len(events)
        assert not store.resource_reservations()


@pytest.mark.parametrize("arm_id", [
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
])
def test_sqlite_reducer_matches_independent_simpy_pilot_trace(tmp_path, arm_id):
    """Crash recovery alone is not proof of operational mechanism equivalence."""
    from sose.organizational.od5a_mechanisms import run_od5a_pilot

    arm = _arms()[arm_id]
    reference = run_od5a_pilot(arm=arm, items=_items(), horizon=100.0)
    durable_state, events, reservations = _finish(tmp_path / "conformance.db", arm_id)
    assert not reservations and durable_state["done"]
    reference_trace = sorted((
        event.kind, event.item_id, event.source, event.target,
        event.start, event.end, event.resource_instance,
    ) for event in reference.events)
    durable_trace = sorted((
        event.payload["kind"], event.payload["item_id"],
        event.payload["source"], event.payload["target"],
        event.payload["start"], event.payload["end"],
        event.payload["resource"],
    ) for event in events if event.payload["kind"] != "arrival")
    assert durable_trace == reference_trace
    assert durable_state["completed"] == dict(reference.completed_at)


def test_two_job_scoped_organizations_advance_without_cross_job_reservations(tmp_path):
    shared = tmp_path / "organizations.db"
    with SQLitePersistence(shared) as store:
        initialize_od5a_durable(store, job_id="org-A", arm=_arms()["baseline"], items=_items())
        initialize_od5a_durable(store, job_id="org-B", arm=_arms()["capacity_only"], items=_items())
        for _ in range(200):
            left = advance_od5a_durable(store, job_id="org-A")
            right = advance_od5a_durable(store, job_id="org-B")
            names = [reservation.resource_name for reservation in store.resource_reservations()]
            assert len(names) == len(set(names))
            assert all(name.startswith(("org-A/", "org-B/")) for name in names)
            if not left and not right:
                break
        else:
            pytest.fail("independent organizations did not terminate")
        assert od5a_durable_state(store, job_id="org-A")["done"]
        assert od5a_durable_state(store, job_id="org-B")["done"]
        assert not store.resource_reservations()
        assert {e.correlation_id for e in store.events()
                if e.name.startswith("od5a.")} == {"org-A", "org-B"}


def _die_before_semantic_commit(path: str):
    """Force OS death inside an open UoW, after event append but before COMMIT."""
    import os
    import sose.organizational.od5a_durable as durable

    original_emit = durable._emit

    def kill_after_append(*args, **kwargs):
        original_emit(*args, **kwargs)
        os._exit(98)

    durable._emit = kill_after_append
    with SQLitePersistence(path) as store:
        advance_od5a_durable(store, job_id="od5a-test")


def test_real_worker_death_before_commit_rolls_back_entire_semantic_action(tmp_path):
    path = tmp_path / "before_commit.db"
    before = _finish(path, "baseline", stop_after=2)
    child = get_context("spawn").Process(target=_die_before_semantic_commit, args=(str(path),))
    child.start()
    child.join(timeout=20)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("worker did not exit during uncommitted event append")
    assert child.exitcode == 98
    with SQLitePersistence(path) as store:
        after = (
            od5a_durable_state(store, job_id="od5a-test"),
            od5a_durable_events(store, job_id="od5a-test"),
            store.resource_reservations(),
        )
    assert after == before
    assert _finish(path, "baseline") == _finish(tmp_path / "continuous.db", "baseline")


def test_durable_rejects_coordination_below_datetime_resolution(tmp_path):
    baseline = _arms()["baseline"]
    payload = baseline.model.canonical_payload()
    payload["parameters"]["coordination_latency"] = 0.0000001
    invalid = _compiled_baseline_for(
        ModelSpec.model_validate(payload), removed=("a", "c"),
    )
    with SQLitePersistence(tmp_path / "precision.db") as store:
        with pytest.raises(ValueError, match="microsecond"):
            initialize_od5a_durable(store, job_id="precision", arm=invalid, items=_items())
        assert store.entity("od5a_durable_pilot", "precision") is None


@pytest.mark.parametrize("arm_id", [
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
])
def test_fractional_exact_microseconds_match_simpy_after_addition(tmp_path, arm_id):
    """Valid 0.1 + 0.2 must finish at exactly 0.3, not persisted binary noise."""
    from sose.organizational.od5a_mechanisms import run_od5a_pilot

    arm = _arms()[arm_id]
    work = (
        OD5aWorkItem("fractional", 0.1, (("a", 0.2), ("b", 0.2), ("c", 0.2))),
        OD5aWorkItem("next", 0.3, (("a", 0.1), ("b", 0.1), ("c", 0.1))),
    )
    expected = run_od5a_pilot(arm=arm, items=work, horizon=100.0)
    with SQLitePersistence(tmp_path / "fractional.db") as store:
        initialize_od5a_durable(store, job_id="fractional", arm=arm, items=work)
        for _ in range(100):
            if not advance_od5a_durable(store, job_id="fractional"):
                break
        else:
            pytest.fail("fractional world did not terminate")
        state = od5a_durable_state(store, job_id="fractional")
        events = od5a_durable_events(store, job_id="fractional")
        actual = sorted((
            e.payload["kind"], e.payload["item_id"],
            e.payload["source"], e.payload["target"],
            e.payload["start"], e.payload["end"], e.payload["resource"],
        ) for e in events if e.payload["kind"] != "arrival")
        expected_trace = sorted((
            e.kind, e.item_id, e.source, e.target, e.start, e.end,
            e.resource_instance,
        ) for e in expected.events)
        assert state["completed"] == dict(expected.completed_at)
        assert actual == expected_trace
        from decimal import Decimal

        assert all(
            (ticks := Decimal(str(e.payload["end"])) * 1_000_000)
            == ticks.to_integral_value()
            for e in events
        )


def test_large_logical_clock_fails_closed_on_float_second_precision_loss(tmp_path):
    """A huge clock plus one microsecond cannot silently become two."""
    arm = _arms()["baseline"]
    work = (
        OD5aWorkItem(
            item_id="far-future", arrival_at=float(2**33),
            service_by_station=(("a", 0.000001), ("b", 1.0), ("c", 1.0)),
        ),
    )
    with SQLitePersistence(tmp_path / "huge.db") as store:
        initialize_od5a_durable(store, job_id="huge", arm=arm, items=work)
        prior = od5a_durable_state(store, job_id="huge")
        with pytest.raises(ValueError, match="round-trip|lossless"):
            advance_od5a_durable(store, job_id="huge")
        assert od5a_durable_state(store, job_id="huge") == prior
        assert not od5a_durable_events(store, job_id="huge")
        assert not store.resource_reservations()


def test_unsorted_declared_dag_edges_preserve_simpy_causal_handoff_order(tmp_path):
    """Coordinator contention must use the same deterministic target order."""
    from sose.organizational.od5a_mechanisms import run_od5a_pilot

    baseline = _arms()["baseline"]
    payload = baseline.model.canonical_payload()
    payload["routing"] = {
        "dependency_edges": [["a", "c"], ["a", "b"], ["b", "c"]],
    }
    model = ModelSpec.model_validate(payload)
    arm = _compiled_baseline_for(model, removed=("a", "c"))
    work = _items()
    expected = run_od5a_pilot(arm=arm, items=work, horizon=100.0)
    with SQLitePersistence(tmp_path / "unsorted.db") as store:
        initialize_od5a_durable(store, job_id="unsorted", arm=arm, items=work)
        for _ in range(150):
            if not advance_od5a_durable(store, job_id="unsorted"):
                break
        else:
            pytest.fail("unordered-DAG workload failed to terminate")
        state = od5a_durable_state(store, job_id="unsorted")
        actual = sorted((
            e.payload["kind"], e.payload["item_id"],
            e.payload["source"], e.payload["target"],
            e.payload["start"], e.payload["end"], e.payload["resource"],
        ) for e in od5a_durable_events(store, job_id="unsorted")
            if e.payload["kind"] != "arrival")
        reference = sorted((
            e.kind, e.item_id, e.source, e.target,
            e.start, e.end, e.resource_instance,
        ) for e in expected.events)
        assert state["completed"] == dict(expected.completed_at)
        assert actual == reference


def test_foreign_reservation_blocks_same_physical_instance_before_grant(tmp_path):
    """A second reservation ID must not defeat capacity-one physical truth."""
    from datetime import datetime, timezone
    from sose.core.runtime import ResourceReservation

    path = tmp_path / "foreign-lease.db"
    arm = _arms()["baseline"]
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(store, job_id="od5a-test", arm=arm, items=_items())
        with store.transaction() as uow:
            uow.save_resource_reservation(ResourceReservation(
                reservation_id="external-lease", request_id="external-request",
                resource_name="od5a-test/execution:0",
                acquired_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            ))
        prior = od5a_durable_state(store, job_id="od5a-test")
        before_events = od5a_durable_events(store, job_id="od5a-test")
        with pytest.raises(RuntimeError, match="resource|reservation|capacity"):
            advance_od5a_durable(store, job_id="od5a-test")
        assert od5a_durable_state(store, job_id="od5a-test") == prior
        assert od5a_durable_events(store, job_id="od5a-test") == before_events
        reservations = store.resource_reservations()
        assert len(reservations) == 1
        assert reservations[0].reservation_id == "external-lease"


def test_hand_constructed_compiled_label_cannot_claim_scientific_admission(tmp_path):
    """Matching arm label and hash do not prove the preregistered compiler ran."""
    arm = _arms()["baseline"]
    forged_baseline = OD5aCompiledArm(
        "baseline", arm.model, OD5aChargedCost(8, 1, 1, 1), None,
    )
    forged_treatment = OD5aCompiledArm(
        "capacity_only", _arms()["capacity_only"].model, None, None,
    )
    for index, illegal in enumerate((forged_baseline, forged_treatment)):
        path = tmp_path / f"forged-{index}.db"
        with SQLitePersistence(path) as store:
            with pytest.raises(ValueError, match="compiled|admission"):
                initialize_od5a_durable(
                    store, job_id="forged", arm=illegal, items=_items(),
                )
            assert store.entity("od5a_durable_pilot", "forged") is None
