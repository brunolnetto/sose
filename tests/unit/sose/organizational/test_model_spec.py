from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from sose.organizational.model_spec import (
    EvidenceClass,
    InterventionClass,
    ModelIntervention,
    ModelSpec,
)


def _spec(**overrides: object) -> ModelSpec:
    payload: dict[str, object] = {
        "version": "1",
        "stations": {"review": {"capacity": 1}},
        "actors": {"alice": {"capabilities": ["review"]}},
        "routing": {"submitted": "review"},
        "authority": {"approve": ["alice"]},
        "quality_gates": {"review": {"detect_probability": 0.8}},
        "policies": {"dispatch": "fifo"},
        "demand": {"kind": "open", "arrival_rate": 2.0},
        "costs": {"alice_hour": 10.0},
        "agency": {"level": "A0"},
        "parameters": {"review.capacity": 1},
        "parameter_evidence": {"review.capacity": EvidenceClass.ASSUMED},
    }
    payload.update(overrides)
    return ModelSpec.model_validate(payload)


def test_canonical_hash_is_stable_across_mapping_order() -> None:
    first = _spec(stations={"review": {"capacity": 1, "calendar": "weekday"}})
    second = _spec(stations={"review": {"calendar": "weekday", "capacity": 1}})

    assert first.canonical_json() == second.canonical_json()
    assert first.model_spec_hash == second.model_spec_hash


def test_hash_changes_when_structure_changes() -> None:
    first = _spec()
    second = _spec(stations={"review": {"capacity": 2}})

    assert first.model_spec_hash != second.model_spec_hash


def test_every_declared_parameter_requires_an_evidence_class() -> None:
    with pytest.raises(ValueError, match="evidence class"):
        _spec(parameter_evidence={})


def test_model_spec_is_deeply_immutable_after_validation() -> None:
    spec = _spec()
    original_hash = spec.model_spec_hash

    with pytest.raises(TypeError):
        spec.stations["review"] = {"capacity": 2}  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.stations["review"]["capacity"] = 2  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        spec.actors["alice"]["capabilities"].append("security")  # type: ignore[index,union-attr]

    assert spec.model_spec_hash == original_hash


def test_model_spec_rejects_non_json_values_and_non_finite_floats() -> None:
    with pytest.raises(ValidationError):
        _spec(stations={"review": {"value": object()}})

    with pytest.raises(ValidationError):
        _spec(stations={"review": {"capacity": math.inf}})


def test_structural_diff_reports_json_pointer_paths() -> None:
    before = _spec(
        stations={"review": {"capacity": 1}, "legacy": {"capacity": 1}},
        routing={"submitted": "legacy"},
    )
    after = _spec(
        stations={"review": {"capacity": 2}, "security": {"capacity": 1}},
        routing={"submitted": "review"},
    )

    diff = before.diff(after)

    assert diff.added == {"/stations/security": {"capacity": 1}}
    assert diff.removed == {"/stations/legacy": {"capacity": 1}}
    assert diff.changed["/stations/review/capacity"] == (1, 2)
    assert diff.changed["/routing/submitted"] == ("legacy", "review")


def test_json_pointer_can_address_keys_containing_dots() -> None:
    before = _spec()
    intervention = ModelIntervention(
        intervention_id="raise-capacity-v1",
        intervention_class=InterventionClass.CAPACITY,
        set_values={"/parameters/review.capacity": 2},
    )

    after, diff = intervention.apply(before)

    assert after.parameters["review.capacity"] == 2
    assert diff.changed["/parameters/review.capacity"] == (1, 2)


def test_intervention_is_a_versioned_spec_transformation() -> None:
    before = _spec()
    intervention = ModelIntervention(
        intervention_id="delegate-review-v1",
        intervention_class=InterventionClass.STRUCTURAL,
        set_values={
            "/authority/approve": ["alice", "bob"],
            "/actors/bob": {"capabilities": ["review"]},
        },
        remove_paths=[],
        mechanisms_added=["delegated_approval"],
        mechanisms_removed=[],
        mechanisms_changed=["approval_authority"],
        operating_cost=5.0,
        transition_cost=20.0,
        transition_time=2.0,
    )

    after, diff = intervention.apply(before)

    assert after.model_spec_hash != before.model_spec_hash
    assert after.authority["approve"] == ("alice", "bob")
    assert after.actors["bob"] == {"capabilities": ("review",)}
    assert diff.added["/actors/bob"] == {"capabilities": ["review"]}
    assert diff.changed["/authority/approve"] == (["alice"], ["alice", "bob"])
