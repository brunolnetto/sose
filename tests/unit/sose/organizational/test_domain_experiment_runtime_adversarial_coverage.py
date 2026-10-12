"""Domain experiment admission and evidence falsification paths.

Scientific claims are rejected before a report may confuse a malformed
protocol, evidence binding or non-finite metric with an experimental finding.
"""
from __future__ import annotations

from math import inf, nan
import pytest

from sose.organizational.domain_experiment_runtime import (
    DomainExperimentPlan, DomainExecutionRequest, DomainExperimentResult,
    ExperimentObservation, RegimeReference, GroundTruthAssessment,
    WorldReferenceRecord, _assess_claim, _validate_json, _freeze_json,
    _thaw_json, evidence_hash,
)
from sose.organizational.domain_reference import (
    GroundTruthClaim, GroundTruthKind, GroundTruthTargetKind,
    GroundTruthComparisonRule, GroundTruthComparisonKind,
)
from sose.organizational.synthetic_queue_reference import (
    QueueReferenceDomain, build_queue_reference_plan_v1,
)


@pytest.mark.parametrize("metrics", [{"": 1.0}, {"lead_time": inf}, {"lead_time": nan}])
def test_observation_rejects_blank_or_non_finite_metrics(metrics):
    with pytest.raises(ValueError, match="metric"):
        ExperimentObservation(metrics=metrics)


def test_observation_serialization_cannot_mutate_source_truth():
    input_metrics = {"z": 2.0, "a": 1.0}
    observation = ExperimentObservation(metrics=input_metrics)
    input_metrics["z"] = 99.0
    assert observation.canonical_payload() == {"metrics": {"a": 1.0, "z": 2.0}}
    assert observation.model_dump(mode="json") == {"metrics": {"a": 1.0, "z": 2.0}}
    with pytest.raises(TypeError):
        observation.metrics["a"] = 20.0


@pytest.mark.parametrize(("value", "reason"), [
    ({"a": inf}, "non-finite JSON"),
    ({"a": [1, nan]}, "non-finite JSON"),
    ({"nested": {3: "value"}}, "must be a string"),
    ({"a": {1, 2}}, "not JSON-compatible"),
])
def test_regime_metadata_rejects_unserializable_or_non_finite_truth(value, reason):
    with pytest.raises(ValueError, match=reason):
        RegimeReference(label="stable", metadata=value)


def test_nested_regime_reference_is_deeply_immutable_and_json_roundtrips():
    raw = {"operations": [{"rate": 1.0}], "trace": [1, 2]}
    regime = RegimeReference(label="busy", metadata=raw)
    raw["operations"][0]["rate"] = 88.0
    assert regime.canonical_payload()["metadata"]["operations"] == [{"rate": 1.0}]
    assert regime.model_dump(mode="json")["metadata"]["trace"] == [1, 2]
    with pytest.raises(TypeError):
        regime.metadata["untrusted"] = True
    assert _thaw_json(_freeze_json(raw)) == raw


def test_plan_must_have_one_configuration_per_registered_agency_level():
    base = build_queue_reference_plan_v1(QueueReferenceDomain())
    with pytest.raises(ValueError, match="exactly one agency configuration"):
        DomainExperimentPlan(
            protocol=base.protocol, design_seed=base.design_seed,
            root_seed=base.root_seed,
            agency_configurations=(base.agency_configurations[0],),
        )


def test_foreign_world_cannot_be_executed_under_different_protocol():
    base = build_queue_reference_plan_v1(QueueReferenceDomain())
    # A forged world/protocol binding must fail even if the world itself
    # remains a valid immutable model instance.
    # Construct a real world without executing a simulation: only the
    # protocol/world identity mismatch is under test here.
    from sose.organizational.domain_experiment import build_experiment_worlds

    protocol = base.protocol.model_copy(update={"sample_size": 2})
    worlds = build_experiment_worlds(
        reference=QueueReferenceDomain(),
        protocol=protocol,
        agency_configurations=base.agency_configurations,
        design_seed=base.design_seed,
    )
    forged_world = worlds[0].model_copy(update={"protocol_hash": "wrong-protocol"})
    with pytest.raises(ValueError, match="does not belong"):
        DomainExecutionRequest(protocol=protocol, world=forged_world, root_seed=1, replication=0)


@pytest.mark.parametrize("value", [inf, nan, {1: 2}, {"untrusted": set()}, object()])
def test_evidence_hash_must_reject_noncanonical_json(value):
    with pytest.raises(ValueError, match="JSON|not JSON-compatible"):
        evidence_hash(value)


def test_evidence_hash_accepts_domain_canonical_payload_not_identity():
    class Canonical:
        def canonical_payload(self):
            return {"x": [1, None, {"y": True}]}

    assert evidence_hash(Canonical()) == evidence_hash({"x": [1, None, {"y": True}]})


def _claim(target_kind=GroundTruthTargetKind.METRIC, *,
           expected=10.0, kind=GroundTruthComparisonKind.EXACT, tolerance=None,
           target_name="lead_time"):
    return GroundTruthClaim(
        claim_id="claim:one",
        kind=GroundTruthKind.ANALYTICAL,
        target_kind=target_kind,
        target_name=target_name,
        expected=expected,
        comparison_rule=GroundTruthComparisonRule(kind=kind, tolerance=tolerance),
        assumptions=("configured-load",),
        eligibility_rule="structural protocol",
        eligible=True, provenance=("reference",),
    )


@pytest.mark.parametrize(("target_kind", "expected", "kind", "tolerance", "reason", "passed"), [
    (GroundTruthTargetKind.REGIME, "busy", GroundTruthComparisonKind.EXACT, None, None, True),
    (GroundTruthTargetKind.REGIME, 100, GroundTruthComparisonKind.EXACT, None, "exact string", None),
    (GroundTruthTargetKind.REGIME, 100., GroundTruthComparisonKind.LOWER_BOUND, None, "exact string", None),
    (GroundTruthTargetKind.INVARIANT, True, GroundTruthComparisonKind.EXACT, None, "not assessed", None),
    (GroundTruthTargetKind.METRIC, True, GroundTruthComparisonKind.EXACT, None, "not numeric", None),
    (GroundTruthTargetKind.METRIC, 10., GroundTruthComparisonKind.EXACT, None, None, True),
    (GroundTruthTargetKind.METRIC, 10.1, GroundTruthComparisonKind.ABSOLUTE_TOLERANCE, 0.2, None, True),
    (GroundTruthTargetKind.METRIC, 11., GroundTruthComparisonKind.RELATIVE_TOLERANCE, 0.1, None, True),
    (GroundTruthTargetKind.METRIC, 9., GroundTruthComparisonKind.LOWER_BOUND, None, None, True),
    (GroundTruthTargetKind.METRIC, 9., GroundTruthComparisonKind.UPPER_BOUND, None, None, False),
])
def test_claim_assessment_preserves_target_and_comparison_semantics(
    target_kind, expected, kind, tolerance, reason, passed,
):
    claim = _claim(target_kind, expected=expected, kind=kind, tolerance=tolerance)
    assessment = _assess_claim(
        world_hash="world", replication=0, claim=claim,
        observation=ExperimentObservation(metrics={"lead_time": 10.0}),
        regime=RegimeReference(label="busy"),
    )
    assert assessment.eligible
    assert assessment.passed is passed
    if reason is not None:
        assert reason in assessment.reason


def test_missing_metric_and_ineligible_claim_are_not_counted_as_failures():
    missing = _assess_claim(
        world_hash="world", replication=0,
        claim=_claim(target_name="unavailable"),
        observation=ExperimentObservation(metrics={"lead_time": 10.0}),
        regime=RegimeReference(label="busy"),
    )
    assert missing.passed is None and "does not expose" in missing.reason
    ineligible = _claim().model_copy(update={
        "eligible": False, "ineligibility_reason": "not identified",
    })
    assessment = _assess_claim(
        world_hash="world", replication=0,
        claim=ineligible,
        observation=ExperimentObservation(metrics={"lead_time": 10.0}),
        regime=RegimeReference(label="busy"),
    )
    assert not assessment.eligible
    assert assessment.passed is None and assessment.reason == "not identified"
