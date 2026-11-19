"""#590: adversarial Gate-A scientific conformance with REAL reference executions.

Mutations represent a corrupt producer/serialized world AFTER the framework's
type validation. The Gate-A conformance layer must independently reject them,
even when a forged model bypasses its Pydantic constructor using model_copy.
No fabricated successful experimental observation or threshold exception.
"""
from __future__ import annotations

from math import nan

import pytest

from sose.organizational.domain_experiment import build_experiment_worlds
from sose.organizational.domain_experiment_conformance import (
    _crn_pairing,
    _deterministic_execution,
    _exogenous_binding,
    _ground_truth_integrity,
    _observation_completeness,
)
from sose.organizational.synthetic_queue_reference import (
    QueueReferenceDomain,
    build_queue_reference_plan_v1,
)


def _worlds():
    reference = QueueReferenceDomain()
    plan = build_queue_reference_plan_v1(reference)
    worlds = build_experiment_worlds(
        reference=reference,
        protocol=plan.protocol,
        agency_configurations=plan.agency_configurations,
        design_seed=plan.design_seed,
    )
    assert worlds
    return reference, plan, worlds


def _first_design(worlds):
    group = tuple(w for w in worlds if w.design_index == worlds[0].design_index)
    assert any(w.arm_id == "baseline" for w in group)
    assert len(group) > 1
    return group


def test_exogenous_gate_accepts_original_reference_worlds():
    reference, _, worlds = _worlds()
    check = _exogenous_binding(reference.descriptor, worlds)
    assert check.name == "exogenous-binding"
    assert check.passed


def test_exogenous_gate_rejects_undeclared_parameter_even_with_legal_looking_value():
    reference, _, worlds = _worlds()
    first = worlds[0]
    forged = first.model_copy(update={
        "exogenous_parameters": {**dict(first.exogenous_parameters), "undisclosed_load": 2.0},
    })
    check = _exogenous_binding(reference.descriptor, (forged,))
    assert not check.passed
    assert "DOE coordinates drift" in check.detail


def test_exogenous_gate_refuses_design_point_drift_between_treatment_and_baseline():
    reference, _, worlds = _worlds()
    group = list(_first_design(worlds))
    original = group[1]
    name = next(iter(original.exogenous_parameters))
    group[1] = original.model_copy(update={
        "exogenous_parameters": {
            **dict(original.exogenous_parameters),
            name: original.exogenous_parameters[name] + 0.125,
        },
    })
    check = _exogenous_binding(reference.descriptor, tuple(group))
    assert not check.passed


def test_exogenous_gate_requires_baseline_among_comparison_arms():
    reference, _, worlds = _worlds()
    treatments = tuple(w for w in _first_design(worlds) if w.arm_id != "baseline")
    assert treatments
    check = _exogenous_binding(reference.descriptor, treatments)
    assert not check.passed


@pytest.mark.parametrize("invalid_value", [True, "not-numeric", nan, 1_000_000.0])
def test_baseline_spec_must_bind_original_sampled_doe_coordinate(invalid_value):
    reference, _, worlds = _worlds()
    baseline = next(w for w in _first_design(worlds) if w.arm_id == "baseline")
    name = next(iter(baseline.exogenous_parameters))
    corrupted_spec = baseline.model_spec.model_copy(update={
        "parameters": {**dict(baseline.model_spec.parameters), name: invalid_value},
    })
    corrupted_world = baseline.model_copy(update={"model_spec": corrupted_spec})
    assert not _exogenous_binding(reference.descriptor, (corrupted_world,)).passed


def test_crn_grouping_refuses_metadata_divergence_within_same_design():
    reference, plan, worlds = _worlds()
    group = list(_first_design(worlds))
    group[1] = group[1].model_copy(update={"crn_group": "foreign:seed"})
    check = _crn_pairing(reference, plan, tuple(group))
    assert check.name == "crn-pairing"
    assert not check.passed


def test_real_reference_with_nondeterministic_evidence_digest_is_rejected():
    class AlteredDigest(QueueReferenceDomain):
        def __init__(self):
            self.calls = 0

        def execute(self, request):
            result = super().execute(request)
            self.calls += 1
            # Corrupt only the evidence *certificate* after real execution.
            return result.model_copy(update={
                "evidence_hash": ("a" if self.calls % 2 else "b") * 64,
            })

    reference, plan, worlds = _worlds()
    check = _deterministic_execution(AlteredDigest(), plan, _first_design(worlds))
    assert check.name == "deterministic-execution"
    assert not check.passed


def test_observation_gate_refuses_missing_metrics_after_real_execution():
    class EmptyObservation(QueueReferenceDomain):
        def observation(self, result):
            observation = super().observation(result)
            # Corrupted serialized observation from a faulty adapter.
            return observation.model_copy(update={"metrics": {}})

    reference, plan, worlds = _worlds()
    check = _observation_completeness(
        EmptyObservation(), plan, _first_design(worlds),
    )
    assert check.name == "observation-completeness"
    assert not check.passed


@pytest.mark.parametrize("violation", [
    "missing_claims",
    "duplicate_claim_identity",
    "eligible_with_disqualification",
    "ineligible_without_reason",
])
def test_ground_truth_gate_refuses_corrupt_eligibility_or_duplicate_claims(violation):
    class CorruptedClaims(QueueReferenceDomain):
        def ground_truth(self, world):
            claims = super().ground_truth(world)
            assert claims
            if violation == "missing_claims":
                return ()
            if violation == "duplicate_claim_identity":
                return (*claims, claims[0])
            first = claims[0]
            if violation == "eligible_with_disqualification":
                assert first.eligible
                poisoned = first.model_copy(update={
                    "ineligibility_reason": "a forged exclusion",
                })
            else:
                poisoned = first.model_copy(update={
                    "eligible": False, "ineligibility_reason": None,
                })
            return (poisoned, *claims[1:])

    reference, plan, worlds = _worlds()
    check = _ground_truth_integrity(
        CorruptedClaims(), plan, _first_design(worlds),
    )
    assert check.name == "ground-truth-integrity"
    assert not check.passed
    assert "invalid" in check.detail
