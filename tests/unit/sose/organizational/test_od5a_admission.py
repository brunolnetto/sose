"""OD-5a mechanism admission TDD; these tests do not execute confirmatory worlds."""
from __future__ import annotations

from dataclasses import replace

import pytest

from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import (
    ALL_ARMS,
    TREATMENT_ARMS,
    OD5aChargedCost,
    compile_od5a_arms,
)


def _baseline() -> ModelSpec:
    return ModelSpec(
        demand={"arrival_process": "exogenous", "arrival_identity": "fixed"},
        stations={"a": {}, "b": {}, "c": {}},
        calendars={"a": "standard", "b": "standard", "c": "standard"},
        routing={
            "dependency_edges": [["a", "b"], ["b", "c"], ["a", "c"]],
            "policy": "first_in_first_out",
        },
        costs={"baseline_operating_cost": 4.0},
        parameters={
            "arrival_rate": 0.8,
            "execution_slots": 1,
            "coordination_latency": 2.0,
            "service_cv": 0.7,
        },
        parameter_evidence={
            key: EvidenceClass.ASSUMED for key in (
                "arrival_rate", "execution_slots", "coordination_latency", "service_cv",
            )
        },
    )


def _costs() -> dict[str, OD5aChargedCost]:
    return {
        "capacity_only": OD5aChargedCost(7.0, 2.0, 1.0, 1.0),
        "topology_only": OD5aChargedCost(6.0, 3.0, 1.0, 1.0),
        "coordination_only": OD5aChargedCost(8.0, 1.0, 1.0, 1.0),
        "structural_combined": OD5aChargedCost(5.0, 4.0, 1.0, 1.0),
    }


def _admit(**overrides):
    args = {
        "baseline": _baseline(),
        "expanded_capacity": 2,
        "removed_dependency_edges": (("a", "c"),),
        "reduced_coordination_latency": 1.2,
        "declared_costs": _costs(),
    }
    args.update(overrides)
    return compile_od5a_arms(**args)


def test_compiler_emits_exact_five_predeclared_arms_at_matched_total_cost() -> None:
    result = _admit()
    assert tuple(arm.arm_id for arm in result) == ALL_ARMS
    assert tuple(result[i].arm_id for i in range(1, 5)) == TREATMENT_ARMS
    assert result[0].intervention is None
    assert result[0].charged_cost is None
    assert {arm.charged_cost.total for arm in result[1:]} == {10.0}
    assert len({arm.model.model_spec_hash for arm in result}) == 5
    assert len({arm.admission_hash for arm in result}) == 5
    assert [arm.admission_hash for arm in result] == [
        arm.admission_hash for arm in _admit()
    ]


def test_intervention_changes_only_allowed_mechanisms_and_preserves_exogenous_source():
    results = {arm.arm_id: arm for arm in _admit()}
    baseline = results["baseline"].model.canonical_payload()
    capacity = results["capacity_only"].model.canonical_payload()
    topology = results["topology_only"].model.canonical_payload()
    coordination = results["coordination_only"].model.canonical_payload()
    combined = results["structural_combined"].model.canonical_payload()
    assert baseline["routing"]["dependency_edges"] == [
        ["a", "b"], ["b", "c"], ["a", "c"],
    ]
    assert capacity["routing"] == baseline["routing"]
    assert capacity["parameters"]["execution_slots"] == 2
    assert capacity["parameters"]["coordination_latency"] == 2.0
    assert topology["parameters"] == baseline["parameters"]
    assert topology["routing"]["dependency_edges"] == [["a", "b"], ["b", "c"]]
    assert coordination["routing"] == baseline["routing"]
    assert coordination["parameters"]["coordination_latency"] == 1.2
    assert coordination["parameters"]["execution_slots"] == 1
    assert combined["routing"]["dependency_edges"] == topology["routing"]["dependency_edges"]
    assert combined["parameters"]["coordination_latency"] == 1.2
    assert combined["parameters"]["execution_slots"] == 1
    for arm in results.values():
        model = arm.model.canonical_payload()
        for section in ("demand", "actors", "calendars", "costs", "parameter_evidence"):
            assert model[section] == baseline[section]
        assert model["parameters"]["arrival_rate"] == 0.8
        assert model["parameters"]["service_cv"] == 0.7
        assert model["routing"]["policy"] == "first_in_first_out"


def test_immutable_baseline_and_full_priced_transition_are_hash_addressed():
    baseline = _baseline()
    before_hash = baseline.model_spec_hash
    admitted = _admit(baseline=baseline)
    assert baseline.model_spec_hash == before_hash
    assert baseline.routing["dependency_edges"] == (("a", "b"), ("b", "c"), ("a", "c"))
    cost = admitted[1].charged_cost
    assert cost.total == 7 + 2 + 1
    changed_costs = _costs()
    changed_costs["capacity_only"] = replace(cost, operating_cost_at_horizon=6.8)
    with pytest.raises(ValueError, match="matched lifecycle cost"):
        _admit(declared_costs=changed_costs)


@pytest.mark.parametrize("case,match", [
    ("missing", "exactly four"),
    ("extra", "exactly four"),
    ("unmatched", "matched lifecycle cost"),
    ("zero", "positive priced budget"),
    ("unpriced_time", "positive shadow price"),
    ("nan", "finite and nonnegative"),
    ("overflow", "total lifecycle budget must be finite"),
    ("wrong_type", "typed charged cost ledger"),
])
def test_fail_closed_cost_admission(case, match):
    costs = _costs()
    if case == "missing":
        del costs["structural_combined"]
    elif case == "extra":
        costs["undocumented_arm"] = costs["capacity_only"]
    elif case == "unmatched":
        costs["topology_only"] = replace(costs["topology_only"], operating_cost_at_horizon=6.7)
    elif case == "zero":
        costs["capacity_only"] = OD5aChargedCost(0, 0, 0, 0)
    elif case == "unpriced_time":
        with pytest.raises(ValueError, match=match):
            OD5aChargedCost(10, 0, 1, 0)
        return
    elif case == "nan":
        with pytest.raises(ValueError, match=match):
            OD5aChargedCost(float("nan"), 0, 0, 1)
        return
    elif case == "overflow":
        with pytest.raises(ValueError, match=match):
            OD5aChargedCost(1e308, 1e308, 0, 1)
        return
    elif case == "wrong_type":
        costs["topology_only"] = {"total": 10}
    with pytest.raises(ValueError, match=match):
        _admit(declared_costs=costs)


@pytest.mark.parametrize("changes,match", [
    ({"expanded_capacity": 1}, "strictly increase"),
    ({"expanded_capacity": 1.5}, "strictly increase"),
    ({"reduced_coordination_latency": 2.0}, "strictly reduce"),
    ({"reduced_coordination_latency": -0.1}, "strictly reduce"),
    ({"removed_dependency_edges": ()}, "nonempty and unique"),
    ({"removed_dependency_edges": (("a", "c"), ("a", "c"))}, "nonempty and unique"),
    ({"removed_dependency_edges": (("c", "a"),)}, "cannot invent"),
    ({"removed_dependency_edges": (("a", "b"), ("b", "c"), ("a", "c"))}, "cannot eliminate"),
    ({"budget_tolerance_fraction": 0.02}, "cannot exceed"),
])
def test_fail_closed_unregistered_mechanism_manipulation(changes, match):
    with pytest.raises(ValueError, match=match):
        _admit(**changes)


def test_dependency_cycle_or_duplicate_is_not_a_valid_baseline():
    b = _baseline().canonical_payload()
    b["routing"]["dependency_edges"] = [["a", "b"], ["b", "a"]]
    with pytest.raises(ValueError, match="acyclic"):
        _admit(baseline=ModelSpec.model_validate(b))
    b["routing"]["dependency_edges"] = [["a", "b"], ["a", "b"]]
    with pytest.raises(ValueError, match="duplicate"):
        _admit(baseline=ModelSpec.model_validate(b))


def test_admission_rejects_missing_configured_exogenous_evidence():
    model = _baseline().canonical_payload()
    del model["parameters"]["arrival_rate"]
    del model["parameter_evidence"]["arrival_rate"]
    with pytest.raises(ValueError, match="arrival_rate"):
        _admit(baseline=ModelSpec.model_validate(model))


def test_distinct_shadow_price_changes_admission_hash_at_equal_total_spend():
    costs = _costs()
    before = _admit()
    costs["topology_only"] = OD5aChargedCost(6.0, 2.0, 1.0, 2.0)
    after = _admit(declared_costs=costs)
    assert before[2].charged_cost.total == after[2].charged_cost.total == 10.0
    assert before[2].admission_hash != after[2].admission_hash

def test_admitted_arm_defensively_snapshots_mutable_intervention_payload():
    """Mutation of an inspected Pydantic intervention must not rewrite admitted truth."""
    arm = _admit()[2]
    before_admission_hash = arm.admission_hash
    before_intervention_hash = arm.intervention_hash
    before_model_hash = arm.model.model_spec_hash
    inspection = arm.intervention
    inspection.set_values["/routing/dependency_edges"][0][0] = "tampered"
    inspection.mechanisms_changed.append("unregistered")
    assert arm.admission_hash == before_admission_hash
    assert arm.intervention_hash == before_intervention_hash
    assert arm.model.model_spec_hash == before_model_hash
    assert arm.intervention.apply(_baseline())[0].model_spec_hash == before_model_hash
