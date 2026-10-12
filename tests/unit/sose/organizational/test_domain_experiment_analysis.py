from __future__ import annotations

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_experiment_analysis import (
    ComparisonKind,
    analyze_domain_experiment,
    recompute_domain_experiment_result_hash,
)
from sose.organizational.domain_experiment_runtime import (
    DomainExperimentPlan,
    run_domain_experiment,
)
from sose.organizational.experiment import ReplicationPlan
from sose.organizational.synthetic_queue_reference import (
    QueueReferenceDomain,
    build_queue_reference_plan_v1,
)


def _small_result():
    reference = QueueReferenceDomain()
    base = build_queue_reference_plan_v1(reference)
    protocol = base.protocol.model_copy(
        update={
            "sample_size": 2,
            "replication_plan": ReplicationPlan(
                min_replications=2,
                max_replications=2,
                target_ci_half_width=0.1,
            ),
            "warmup": 5.0,
            "horizon": 80.0,
        }
    )
    plan = DomainExperimentPlan(
        protocol=protocol,
        design_seed=base.design_seed,
        root_seed=base.root_seed,
        agency_configurations=base.agency_configurations,
    )
    return reference, run_domain_experiment(reference=reference, plan=plan)


def test_generic_analysis_builds_intervention_and_agency_effects() -> None:
    reference, result = _small_result()

    report = analyze_domain_experiment(reference=reference, result=result)

    intervention = [
        effect
        for effect in report.effects
        if effect.comparison_kind is ComparisonKind.INTERVENTION
    ]
    agency = [
        effect
        for effect in report.effects
        if effect.comparison_kind is ComparisonKind.AGENCY
    ]

    assert len(intervention) == 2 * 2 * 2 * 2
    assert len(agency) == 2 * 3 * 2
    assert {effect.metric_name for effect in report.effects} == {
        "lead_time",
        "operating_cost",
    }
    assert all(effect.replications == 2 for effect in report.effects)


def test_effects_are_exactly_paired_by_replication_and_crn_group() -> None:
    reference, result = _small_result()

    report = analyze_domain_experiment(reference=reference, result=result)

    assert all(effect.paired for effect in report.effects)
    assert all(effect.control_crn_group == effect.treatment_crn_group for effect in report.effects)
    assert all(effect.paired_standard_error >= 0.0 for effect in report.effects)
    assert all(effect.ci_low <= effect.mean_delta <= effect.ci_high for effect in report.effects)


def test_comparison_eligibility_is_domain_owned_and_outcome_independent() -> None:
    reference, result = _small_result()

    report = analyze_domain_experiment(reference=reference, result=result)
    lead_effects = [
        effect for effect in report.effects if effect.metric_name == "lead_time"
    ]

    assert any(effect.eligible for effect in lead_effects)
    assert any(not effect.eligible for effect in lead_effects)
    assert all(effect.eligibility_basis for effect in lead_effects)
    assert all(
        "lead_time" not in basis
        for effect in lead_effects
        for basis in effect.eligibility_basis
    )


def test_operating_cost_comparisons_remain_eligible_across_regimes() -> None:
    reference, result = _small_result()

    report = analyze_domain_experiment(reference=reference, result=result)
    costs = [
        effect for effect in report.effects if effect.metric_name == "operating_cost"
    ]

    assert costs
    assert all(effect.eligible for effect in costs)


def test_report_is_hash_addressed_and_reproducible() -> None:
    reference, result = _small_result()

    left = analyze_domain_experiment(reference=reference, result=result)
    right = analyze_domain_experiment(reference=reference, result=result)

    assert left == right
    assert left.report_hash == right.report_hash
    assert left.result_hash == result.result_hash


def test_report_contains_no_composite_quality_score() -> None:
    reference, result = _small_result()

    report = analyze_domain_experiment(reference=reference, result=result)
    payload = report.canonical_payload()

    assert "score" not in payload
    assert "composite" not in payload


def test_agency_comparison_uses_a0_as_control_without_rewriting_structure() -> None:
    reference, result = _small_result()

    report = analyze_domain_experiment(reference=reference, result=result)
    agency = next(
        effect
        for effect in report.effects
        if effect.comparison_kind is ComparisonKind.AGENCY
        and effect.metric_name == "lead_time"
        and effect.control_agency_level == AgencyLevel.A0.value
        and effect.treatment_agency_level == AgencyLevel.A1.value
    )

    control = next(world for world in result.worlds if world.world_hash == agency.control_world_hash)
    treatment = next(world for world in result.worlds if world.world_hash == agency.treatment_world_hash)
    assert control.structural_configuration_hash == treatment.structural_configuration_hash


def test_report_rejects_incomplete_paired_run_evidence() -> None:
    reference, result = _small_result()
    malformed = result.model_copy(update={"runs": result.runs[:-1]})
    malformed = malformed.model_copy(
        update={
            "manifest": malformed.manifest.model_copy(
                update={
                    "run_count": len(malformed.runs),
                    "result_hash": recompute_domain_experiment_result_hash(malformed),
                }
            )
        }
    )

    try:
        analyze_domain_experiment(reference=reference, result=malformed)
    except ValueError as exc:
        assert "paired run evidence" in str(exc)
    else:
        raise AssertionError("expected incomplete paired-run rejection")



def test_analysis_rejects_result_payload_that_does_not_match_manifest_hash() -> None:
    reference, result = _small_result()
    run = result.runs[0]
    observation = run.observation.model_copy(
        update={
            "metrics": {
                **dict(run.observation.metrics),
                "lead_time": run.observation.metrics["lead_time"] + 1.0,
            }
        }
    )
    forged_run = run.model_copy(update={"observation": observation})
    runs = (forged_run, *result.runs[1:])
    forged = result.model_copy(update={"runs": runs})

    try:
        analyze_domain_experiment(reference=reference, result=forged)
    except ValueError as exc:
        assert "result hash" in str(exc)
    else:
        raise AssertionError("expected stale result-hash rejection")


def test_small_sample_confidence_interval_uses_student_t() -> None:
    reference, result = _small_result()
    report = analyze_domain_experiment(reference=reference, result=result)
    effect = next(item for item in report.effects if item.paired_standard_error > 0.0)

    critical = (
        (effect.ci_high - effect.ci_low)
        / 2.0
        / effect.paired_standard_error
    )
    assert critical > 10.0


def test_analysis_rejects_replication_seed_mismatch_before_claiming_pairing() -> None:
    reference, result = _small_result()
    run = result.runs[0]
    forged_run = run.model_copy(update={"replication_seed": run.replication_seed + 1})
    runs = (forged_run, *result.runs[1:])
    forged = result.model_copy(update={"runs": runs})
    forged = forged.model_copy(
        update={
            "manifest": forged.manifest.model_copy(
                update={
                    "result_hash": recompute_domain_experiment_result_hash(forged)
                }
            )
        }
    )

    try:
        analyze_domain_experiment(reference=reference, result=forged)
    except ValueError as exc:
        assert "replication seed" in str(exc)
    else:
        raise AssertionError("expected CRN seed mismatch rejection")


def test_analysis_rejects_duplicate_regime_reference_before_mapping() -> None:
    reference, result = _small_result()
    references = (*result.references, result.references[0])
    forged = result.model_copy(update={"references": references})
    forged = forged.model_copy(
        update={
            "manifest": forged.manifest.model_copy(
                update={
                    "result_hash": recompute_domain_experiment_result_hash(forged)
                }
            )
        }
    )

    try:
        analyze_domain_experiment(reference=reference, result=forged)
    except ValueError as exc:
        assert "exactly one regime reference" in str(exc)
    else:
        raise AssertionError("expected duplicate regime-reference rejection")


# Scientific-inference falsification matrix: a forged summary may have a
# perfectly valid digest yet still violate its registered eligibility/CRN
# invariants. These cases explicitly exercise missing analytical branches.

import pytest

from sose.organizational.domain_experiment_analysis import (
    ComparisonEligibility, ExperimentEffect,
    _build_effect, _runs_by_world, _worlds_by_key,
    _student_t_cdf, _student_t_critical, _regularized_incomplete_beta,
)


@pytest.fixture(scope="module")
def analysis_evidence():
    return _small_result()


@pytest.mark.parametrize(("payload", "error"), [
    ({"eligible": True, "basis": ("structure",), "reason": "inadmissible"}, "eligible comparison"),
    ({"eligible": False, "basis": ("structure",), "reason": None}, "ineligible comparison"),
    ({"eligible": True, "basis": ("same", "same"), "reason": None}, "unique"),
])
def test_eligibility_is_non_circular_and_explained(payload, error):
    with pytest.raises(ValueError, match=error):
        ComparisonEligibility.model_validate(payload)
    assert ComparisonEligibility(eligible=True, basis=("physical",)).canonical_payload() == {
        "eligible": True, "basis": ["physical"], "reason": None,
    }


@pytest.mark.parametrize(("corruption", "reason"), [
    ({"paired": False}, "paired CRN"),
    ({"eligible": True, "ineligibility_reason": "invalid"}, "eligible effect"),
    ({"eligible": False, "ineligibility_reason": None}, "ineligible effect"),
    ({"ci_low": 1e99}, "confidence interval"),
    ({"ci_high": -1e99}, "confidence interval"),
    ({"direction": 0, "favorable": True}, "null-region"),
])
def test_report_effect_refuses_unpaired_or_contradictory_inference(
    analysis_evidence, corruption, reason,
):
    reference, result = analysis_evidence
    effect = analyze_domain_experiment(reference=reference, result=result).effects[0]
    forged = {**effect.model_dump(), **corruption}
    with pytest.raises(ValueError, match=reason):
        ExperimentEffect.model_validate(forged)


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("plan_hash", "forged-plan", "plan binding"),
    ("protocol_hash", "forged-protocol", "protocol binding"),
    ("world_count", -1, "world-count binding"),
    ("run_count", -1, "run-count binding"),
    ("domain_reference_hash", "forged-domain", "domain reference binding"),
])
def test_manifest_binding_must_match_authoritative_experiment(
    analysis_evidence, field, value, reason,
):
    reference, result = analysis_evidence
    forged = result.model_copy(update={
        "manifest": result.manifest.model_copy(update={field: value}),
    })
    with pytest.raises(ValueError, match=reason):
        analyze_domain_experiment(reference=reference, result=forged)


def test_duplicate_world_key_is_not_treated_as_an_independent_observation(analysis_evidence):
    _, result = analysis_evidence
    with pytest.raises(ValueError, match="duplicate experiment world identity"):
        _worlds_by_key((*result.worlds, result.worlds[0]))


def test_replications_reject_unknown_worlds_and_duplicate_sample_identity(analysis_evidence):
    _, result = analysis_evidence
    bogus = result.runs[0].model_copy(update={"world_hash": "unknown-world"})
    with pytest.raises(ValueError, match="unknown world"):
        _runs_by_world(worlds=result.worlds, runs=(*result.runs, bogus), replications=2)
    with pytest.raises(ValueError, match="duplicate replication"):
        _runs_by_world(
            worlds=result.worlds,
            runs=(*result.runs, result.runs[0]),
            replications=2,
        )


@pytest.mark.parametrize(("bad_control", "bad_treatment", "reason"), [
    ("alien-crn", None, "shared CRN"),
    (None, "wrong-replication", "replication identities"),
    (None, "missing-preregistered-metric", "preregistered metric"),
])
def test_effect_builder_rejects_causal_or_measurement_mismatch(
    analysis_evidence, bad_control, bad_treatment, reason,
):
    reference, result = analysis_evidence
    world = result.worlds[0]
    control = world
    treatment = world.model_copy(update={"crn_group": "alien-crn"}) if bad_control else world
    all_runs = {
        run.replication: run for run in result.runs if run.world_hash == world.world_hash
    }
    treat_runs = dict(all_runs)
    if bad_treatment == "wrong-replication":
        treat_runs = {100 + k: v for k, v in all_runs.items()}
    with pytest.raises(ValueError, match=reason):
        _build_effect(
            kind=ComparisonKind.INTERVENTION,
            metric_name="nonexistent-metric" if bad_treatment == "missing-preregistered-metric" else "lead_time",
            direction=None,
            equivalence_margin=1.,
            confidence_level=0.95,
            control=control, treatment=treatment,
            control_runs=all_runs, treatment_runs=treat_runs,
            control_regime=result.references[0],
            treatment_regime=result.references[0], reference=reference,
        )


@pytest.mark.parametrize(("level", "df", "reason"), [
    (0.0, 2, "confidence_level"), (1.0, 2, "confidence_level"),
    (0.95, 0, "degrees_freedom"),
])
def test_student_t_rejects_invalid_confidence_or_sample_size(level, df, reason):
    with pytest.raises(ValueError, match=reason):
        _student_t_critical(confidence_level=level, degrees_freedom=df)


def test_student_t_cdf_is_symmetric_including_zero():
    assert _student_t_cdf(0.0, 4) == 0.5
    positive = _student_t_cdf(2.0, 4)
    assert 0.5 < positive < 1.0
    assert _student_t_cdf(-2.0, 4) == pytest.approx(1.0 - positive)


def test_beta_boundary_conditions_preserve_probability_extremes():
    assert _regularized_incomplete_beta(-1.0, 2.0, 3.0) == 0.0
    assert _regularized_incomplete_beta(0.0, 2.0, 3.0) == 0.0
    assert _regularized_incomplete_beta(1.0, 2.0, 3.0) == 1.0
    assert _regularized_incomplete_beta(2.0, 2.0, 3.0) == 1.0
    assert 0.0 < _regularized_incomplete_beta(0.5, 2.0, 3.0) < 1.0
