from __future__ import annotations

from functools import lru_cache

import pytest

from sose.organizational.experiment import ReplicationPlan
from sose.organizational.synthetic_a1_experiment import (
    build_a0_a1_reference_design_v1,
    run_a0_a1_experiment,
)
from sose.organizational.synthetic_a1_report import (
    build_a1_scientific_report_v1,
)


@lru_cache(maxsize=1)
def _small_experiment():
    design = build_a0_a1_reference_design_v1()
    protocol = design.protocol.model_copy(
        update={
            "replication_plan": ReplicationPlan(
                min_replications=2,
                max_replications=2,
                target_ci_half_width=design.protocol.replication_plan.target_ci_half_width,
            )
        }
    )
    worlds = tuple(
        world.model_copy(update={"protocol_hash": protocol.protocol_hash})
        for world in design.worlds
    )
    return run_a0_a1_experiment(
        design=design.model_copy(update={"protocol": protocol, "worlds": worlds})
    )


def test_a1_scientific_report_is_deterministic_and_hash_addressed() -> None:
    experiment = _small_experiment()

    left = build_a1_scientific_report_v1(experiment)
    right = build_a1_scientific_report_v1(experiment)

    assert left == right
    assert left.report_hash == right.report_hash
    assert left.protocol_hash == experiment.design.protocol.protocol_hash


def test_report_pairs_each_intervention_against_same_design_baseline() -> None:
    experiment = _small_experiment()
    report = build_a1_scientific_report_v1(experiment)

    assert len(report.intervention_effects) == 12 * 2
    assert {
        effect.arm_id for effect in report.intervention_effects
    } == {"capacity-up", "automation-rework"}
    assert all(effect.replications == 2 for effect in report.intervention_effects)


def test_report_exposes_regime_displacement_and_adaptation_cost_separately() -> None:
    experiment = _small_experiment()
    report = build_a1_scientific_report_v1(experiment)

    assert report.regime_summary.saturated_to_stable_worlds > 0
    assert report.regime_summary.stable_to_saturated_worlds == 0
    assert report.mean_adaptation_actor_time > 0.0
    assert report.mean_adaptation_cost > 0.0
    assert report.intervention_direction_eligible_count > 0
    assert report.intervention_direction_ineligible_count > 0
    assert report.intervention_direction_agreement_rate is not None
    assert 0.0 <= report.intervention_direction_agreement_rate <= 1.0
    assert (
        report.intervention_direction_agreement_count
        + report.intervention_direction_change_count
        == report.intervention_direction_eligible_count
    )
    assert (
        report.intervention_direction_eligible_count
        + report.intervention_direction_ineligible_count
        == len(report.intervention_effects)
    )


def test_report_keeps_effect_magnitude_and_direction_without_composite_score() -> None:
    experiment = _small_experiment()
    report = build_a1_scientific_report_v1(experiment)

    for effect in report.intervention_effects:
        assert effect.a0_delta_mean_lead_time == effect.a0_delta_mean_lead_time
        assert effect.a1_delta_mean_lead_time == effect.a1_delta_mean_lead_time
        if effect.direction_eligible:
            assert effect.a0_direction in {-1, 0, 1}
            assert effect.a1_direction in {-1, 0, 1}
            assert effect.direction_changed == (effect.a0_direction != effect.a1_direction)
        else:
            assert effect.a0_direction is None
            assert effect.a1_direction is None
            assert effect.direction_changed is None


def test_report_rejects_missing_or_duplicate_paired_evidence() -> None:
    experiment = _small_experiment()

    missing = experiment.model_copy(update={"pairs": experiment.pairs[:-1]})
    with pytest.raises(ValueError, match="complete unique paired evidence"):
        build_a1_scientific_report_v1(missing)

    duplicate = experiment.model_copy(
        update={"pairs": (*experiment.pairs, experiment.pairs[-1])}
    )
    with pytest.raises(ValueError, match="complete unique paired evidence"):
        build_a1_scientific_report_v1(duplicate)
