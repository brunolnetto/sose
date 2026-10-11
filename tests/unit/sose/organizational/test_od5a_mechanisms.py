"""OD-5a toy mechanism falsification only: never use as confirmatory outcomes."""
from __future__ import annotations

from collections import Counter

import pytest

from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_mechanisms import OD5aWorkItem, run_od5a_pilot


def _arms():
    baseline = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"], ["a", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8, "execution_slots": 1,
            "coordination_latency": 2.0, "service_cv": 0.5,
        },
        parameter_evidence={
            name: EvidenceClass.ASSUMED for name in (
                "arrival_rate", "execution_slots",
                "coordination_latency", "service_cv",
            )
        },
    )
    costs = {
        name: OD5aChargedCost(8, 1, 1, 1)
        for name in (
            "capacity_only", "topology_only",
            "coordination_only", "structural_combined",
        )
    }
    return {
        arm.arm_id: arm for arm in compile_od5a_arms(
            baseline=baseline,
            expanded_capacity=2,
            removed_dependency_edges=(("a", "c"),),
            reduced_coordination_latency=1.0,
            declared_costs=costs,
        )
    }


def _work():
    return (
        OD5aWorkItem(
            item_id="x", arrival_at=0.0,
            service_by_station=(("a", 1.0), ("b", 1.0), ("c", 1.0)),
        ),
        OD5aWorkItem(
            item_id="y", arrival_at=0.0,
            service_by_station=(("a", 1.0), ("b", 1.0), ("c", 1.0)),
        ),
    )


def test_five_real_mechanisms_keep_fixed_exogenous_service_and_distinct_resources():
    outcomes = {name: run_od5a_pilot(arm=arm, items=_work(), horizon=80.0)
                for name, arm in _arms().items()}
    assert all(len(outcome.completed_at) == 2 for outcome in outcomes.values())
    assert all(
        sum(event.duration for event in outcome.events if event.kind == "execution") == 6
        for outcome in outcomes.values()
    )
    assert Counter(event.kind for event in outcomes["baseline"].events)["coordination"] == 6
    assert Counter(event.kind for event in outcomes["topology_only"].events)["coordination"] == 4
    assert Counter(event.kind for event in outcomes["coordination_only"].events)["coordination"] == 6
    assert Counter(event.kind for event in outcomes["structural_combined"].events)["coordination"] == 4
    assert sum(
        event.duration for event in outcomes["baseline"].events
        if event.kind == "coordination"
    ) == 12.0
    assert sum(
        event.duration for event in outcomes["coordination_only"].events
        if event.kind == "coordination"
    ) == 6.0
    assert {e.resource_instance for e in outcomes["baseline"].events if e.kind == "execution"} == {"execution:0"}
    assert {e.resource_instance for e in outcomes["capacity_only"].events if e.kind == "execution"} == {"execution:0", "execution:1"}
    for name, result in outcomes.items():
        assert all(event.model_spec_hash == _arms()[name].model.model_spec_hash
                   and event.config_epoch == 0 and event.cause
                   for event in result.events)
        for physical_resource in {e.resource_instance for e in result.events}:
            intervals = sorted(
                (e.start, e.end) for e in result.events if e.resource_instance == physical_resource
            )
            assert all(left[1] <= right[0] for left, right in zip(intervals, intervals[1:]))


def test_transition_downtime_is_not_silent_free_capacity():
    arms = _arms()
    initial = run_od5a_pilot(arm=arms["baseline"], items=_work(), horizon=80)
    treated = run_od5a_pilot(arm=arms["capacity_only"], items=_work(), horizon=80)
    assert min(e.start for e in initial.events if e.kind == "execution") == 0
    assert min(e.start for e in treated.events if e.kind == "execution") >= 1.0


@pytest.mark.parametrize("bad", [
    (OD5aWorkItem("x", 0.0, (("a", 1.0),)),),
    (OD5aWorkItem("x", 0.0, (("a", 1.0), ("b", 1.0), ("c", 1.0))),
     OD5aWorkItem("x", 0.0, (("a", 1.0), ("b", 1.0), ("c", 1.0)))),
])
def test_pilot_rejects_unusable_or_duplicate_workloads(bad):
    with pytest.raises(ValueError):
        run_od5a_pilot(arm=_arms()["baseline"], items=bad, horizon=20)


def test_pilot_refuses_nonpositive_service_and_horizon():
    with pytest.raises(ValueError):
        OD5aWorkItem("bad", 0, (("a", -1),))
    with pytest.raises(ValueError):
        run_od5a_pilot(arm=_arms()["baseline"], items=_work(), horizon=0)
