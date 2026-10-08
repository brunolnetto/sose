from __future__ import annotations

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_experiment_analysis import (
    ComparisonKind,
    analyze_domain_experiment,
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

    try:
        analyze_domain_experiment(reference=reference, result=malformed)
    except ValueError as exc:
        assert "paired run evidence" in str(exc)
    else:
        raise AssertionError("expected incomplete paired-run rejection")
