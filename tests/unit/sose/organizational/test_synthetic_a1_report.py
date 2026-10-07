from __future__ import annotations

from sose.organizational.synthetic_a1_experiment import (
    run_a0_a1_reference_experiment_v1,
)
from sose.organizational.synthetic_a1_report import (
    build_a1_scientific_report_v1,
)


def test_a1_scientific_report_is_deterministic_and_hash_addressed() -> None:
    experiment = run_a0_a1_reference_experiment_v1()

    left = build_a1_scientific_report_v1(experiment)
    right = build_a1_scientific_report_v1(experiment)

    assert left == right
    assert left.report_hash == right.report_hash
    assert left.protocol_hash == experiment.design.protocol.protocol_hash


def test_report_pairs_each_intervention_against_same_design_baseline() -> None:
    experiment = run_a0_a1_reference_experiment_v1()
    report = build_a1_scientific_report_v1(experiment)

    assert len(report.intervention_effects) == 12 * 2
    assert {
        effect.arm_id for effect in report.intervention_effects
    } == {"capacity-up", "automation-rework"}
    assert all(effect.replications == 16 for effect in report.intervention_effects)


def test_report_exposes_regime_displacement_and_adaptation_cost_separately() -> None:
    experiment = run_a0_a1_reference_experiment_v1()
    report = build_a1_scientific_report_v1(experiment)

    assert report.regime_summary.saturated_to_stable_worlds > 0
    assert report.regime_summary.stable_to_saturated_worlds == 0
    assert report.mean_adaptation_actor_time > 0.0
    assert report.mean_adaptation_cost > 0.0
    assert 0.0 <= report.intervention_direction_agreement_rate <= 1.0
    assert (
        report.intervention_direction_agreement_count
        + report.intervention_direction_change_count
        == len(report.intervention_effects)
    )


def test_report_keeps_effect_magnitude_and_direction_without_composite_score() -> None:
    experiment = run_a0_a1_reference_experiment_v1()
    report = build_a1_scientific_report_v1(experiment)

    for effect in report.intervention_effects:
        assert effect.a0_delta_mean_lead_time == effect.a0_delta_mean_lead_time
        assert effect.a1_delta_mean_lead_time == effect.a1_delta_mean_lead_time
        assert effect.a0_direction in {-1, 0, 1}
        assert effect.a1_direction in {-1, 0, 1}
        assert effect.direction_changed == (effect.a0_direction != effect.a1_direction)
