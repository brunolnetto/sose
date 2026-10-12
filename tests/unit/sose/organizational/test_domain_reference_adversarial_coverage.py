"""Reject corrupted exogenous reference declarations and ground-truth claims.

These are specification-level guardrails: malformed assumptions must fail
validation rather than becoming a numerically plausible scientific report.
"""
from __future__ import annotations

import math
import pytest

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_reference import (
    AgencyCapabilitySpec, DomainReferenceDescriptor,
    GroundTruthClaim, GroundTruthKind, GroundTruthTargetKind,
    GroundTruthComparisonRule, GroundTruthComparisonKind,
    _validate_json, _freeze_json, _thaw_json,
    _is_repository_relative, _is_finite_number,
)
from sose.organizational.synthetic_queue_reference import QueueReferenceDomain


def _capability_payload(level):
    # The queue reference intentionally implements only A0/A1. Build an
    # explicit valid A2 specification to falsify its contract-ID uniqueness.
    if level is AgencyLevel.A2:
        return {
            "level": AgencyLevel.A2,
            "capability_id": "synthetic-a2",
            "parameter_ranges": {},
            "defaults": {},
            "compatibility_constraints": (),
            "observation_contract_ids": ("observed",),
            "action_contract_ids": ("actuated",),
        }
    cap = next(
        item for item in QueueReferenceDomain().descriptor.agency_capabilities
        if item.level == level
    )
    return {
        "level": cap.level,
        "capability_id": cap.capability_id,
        "parameter_ranges": dict(cap.parameter_ranges),
        "defaults": dict(cap.defaults),
        "compatibility_constraints": cap.compatibility_constraints,
        "observation_contract_ids": cap.observation_contract_ids,
        "action_contract_ids": cap.action_contract_ids,
    }


def test_agency_capability_rejects_missing_defaults_and_unbounded_values():
    payload = _capability_payload(AgencyLevel.A1)
    name = next(iter(payload["defaults"]))
    default = payload["defaults"][name]
    assert math.isfinite(default)
    for values, reason in [
        ({}, "defaults must exactly match"),
        ({name: float("inf"), **{k:v for k,v in payload["defaults"].items() if k != name}}, "must be finite"),
        ({name: payload["parameter_ranges"][name].high + 1., **{
            k:v for k,v in payload["defaults"].items() if k != name
        }}, "within its range"),
    ]:
        with pytest.raises(ValueError, match=reason):
            AgencyCapabilitySpec.model_validate({**payload, "defaults": values})


def test_agency_contract_cannot_be_activated_by_non_a2_capability():
    payload = _capability_payload(AgencyLevel.A1)
    with pytest.raises(ValueError, match="only valid for A2"):
        AgencyCapabilitySpec.model_validate({
            **payload, "observation_contract_ids": ("observer",),
        })


@pytest.mark.parametrize(("field", "reason"), [
    ("compatibility_constraints", "compatibility constraints"),
    ("observation_contract_ids", "observation contract ids"),
    ("action_contract_ids", "action contract ids"),
])
def test_agency_duplicate_metadata_cannot_silently_change_compliance(field, reason):
    payload = _capability_payload(AgencyLevel.A2)
    if field == "compatibility_constraints":
        duplicated = ("same-constraint", "same-constraint")
    else:
        original = payload[field]
        duplicated = (original[0], original[0])
    with pytest.raises(ValueError, match=reason):
        AgencyCapabilitySpec.model_validate({**payload, field: duplicated})


def test_capability_fingerprint_is_canonical_and_cannot_mutate_after_creation():
    payload = _capability_payload(AgencyLevel.A1)
    cap = AgencyCapabilitySpec.model_validate(payload)
    before = cap.capability_hash
    assert before == cap.capability_hash
    assert cap.canonical_payload()["level"] == AgencyLevel.A1.value
    with pytest.raises(TypeError):
        cap.defaults["forged"] = 9.0


def _claim_payload():
    return {
        "claim_id": "claim:1",
        "kind": GroundTruthKind.ANALYTICAL,
        "target_kind": GroundTruthTargetKind.METRIC,
        "target_name": "lead_time",
        "expected": 1.0,
        "comparison_rule": GroundTruthComparisonRule(kind=GroundTruthComparisonKind.EXACT),
        "assumptions": ("configured-load",),
        "eligibility_rule": "exogenous precondition",
        "eligible": True,
        "ineligibility_reason": None,
        "provenance": ("known-model",),
    }


@pytest.mark.parametrize(("changes", "reason"), [
    ({"eligible": True, "ineligibility_reason": "disqualified"}, "eligible ground-truth"),
    ({"eligible": False, "ineligibility_reason": None}, "ineligible ground-truth"),
    ({"assumptions": ("repeat", "repeat")}, "duplicate ground-truth assumptions"),
    ({"provenance": ("repeat", "repeat")}, "duplicate ground-truth provenance"),
    ({"expected": [1., math.inf]}, "non-finite JSON"),
])
def test_ground_truth_requires_explicit_eligibility_and_auditable_inputs(changes, reason):
    with pytest.raises(ValueError, match=reason):
        GroundTruthClaim.model_validate({**_claim_payload(), **changes})


def test_ground_truth_nonexact_comparison_requires_numeric_expected():
    bad_rule = GroundTruthComparisonRule(
        kind=GroundTruthComparisonKind.LOWER_BOUND,
    )
    with pytest.raises(ValueError, match="finite numeric expected"):
        GroundTruthClaim.model_validate({
            **_claim_payload(),
            "expected": "not-a-number",
            "comparison_rule": bad_rule,
        })


def test_ground_truth_expected_list_is_deep_frozen_and_serializes_without_aliasing():
    original = [1., {"reason": ["x"]}]
    claim = GroundTruthClaim.model_validate({
        **_claim_payload(), "expected": original,
    })
    original[1]["reason"].append("forged")
    assert claim.canonical_payload()["expected"] == [1., {"reason": ["x"]}]
    assert claim.model_dump(mode="json")["expected"] == [1., {"reason": ["x"]}]


def test_reference_descriptor_cannot_duplicate_scientific_ground_truth_kinds():
    descriptor = QueueReferenceDomain().descriptor
    payload = {key: getattr(descriptor, key) for key in type(descriptor).model_fields}
    payload["ground_truth_kinds"] = (
        GroundTruthKind.ANALYTICAL, GroundTruthKind.ANALYTICAL,
    )
    with pytest.raises(ValueError, match="duplicate ground-truth kinds"):
        DomainReferenceDescriptor.model_validate(payload)


@pytest.mark.parametrize(("path", "valid"), [
    ("docs/organization/reference.md", True),
    ("", False), ("../secrets", False), ("/etc/passwd", False),
    ("docs\\private", False), ("a//b", False),
])
def test_reference_specification_paths_must_be_canonical_repository_relative(path, valid):
    assert _is_repository_relative(path) is valid


def test_reference_json_validation_and_immutable_roundtrip():
    values = {"nested": [1, {"empty": []}]}
    _validate_json(values, path="/evidence")
    assert _thaw_json(_freeze_json(values)) == values
    for invalid, reason in [
        ({"missing": float("nan")}, "non-finite JSON"),
        ({123: "key"}, "must be a string"),
        ({"unserializable": {1,2}}, "not JSON-compatible"),
    ]:
        with pytest.raises(ValueError, match=reason):
            _validate_json(invalid, path="/evidence")
    assert not _is_finite_number(True)
    assert not _is_finite_number(float("nan"))
    assert _is_finite_number(0)
