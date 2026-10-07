from __future__ import annotations

from sose.organizational.agency import AgencyLevel
from sose.organizational.ledger import ActorCategory
from sose.organizational.synthetic_a1_experiment import (
    A1RegimeKind,
    build_a0_a1_reference_design_v1,
    classify_a1_regime,
    run_a0_a1_reference_experiment_v1,
)


def test_a0_a1_design_pairs_same_exogenous_worlds_and_crn_groups() -> None:
    design = build_a0_a1_reference_design_v1()

    assert design.protocol.agency_levels == (AgencyLevel.A0, AgencyLevel.A1)
    assert len(design.worlds) == 12 * 3 * 2

    by_key = {}
    for world in design.worlds:
        by_key.setdefault((world.design_index, world.arm_id), {})[
            world.agency_level
        ] = world

    assert all(set(pair) == {AgencyLevel.A0, AgencyLevel.A1} for pair in by_key.values())
    for pair in by_key.values():
        a0 = pair[AgencyLevel.A0]
        a1 = pair[AgencyLevel.A1]
        assert a0.exogenous_parameters == a1.exogenous_parameters
        assert a0.crn_group == a1.crn_group


def test_a1_regime_reference_contains_nominal_adaptive_and_saturated_cases() -> None:
    design = build_a0_a1_reference_design_v1()
    refs = tuple(
        classify_a1_regime(world)
        for world in design.worlds
        if world.agency_level is AgencyLevel.A1
    )

    kinds = {reference.kind for reference in refs}
    assert A1RegimeKind.NOMINAL_STABLE in kinds
    assert A1RegimeKind.ADAPTATION_STABILIZED in kinds
    assert A1RegimeKind.SATURATED in kinds


def test_a0_a1_pairing_preserves_crn_latents() -> None:
    result = run_a0_a1_reference_experiment_v1(replications=2)
    pair = result.pairs[0]

    count = min(len(pair.a0_run.items), len(pair.a1_run.items))
    assert count > 5
    assert [item.arrival_at for item in pair.a0_run.items[:count]] == [
        item.arrival_at for item in pair.a1_run.items[:count]
    ]
    assert [item.service_latent_normal for item in pair.a0_run.items[:count]] == [
        item.service_latent_normal for item in pair.a1_run.items[:count]
    ]
    assert [item.rework_latent_uniform for item in pair.a0_run.items[:count]] == [
        item.rework_latent_uniform for item in pair.a1_run.items[:count]
    ]


def test_a0_a1_experiment_reports_regime_rescue_and_adaptation_cost() -> None:
    result = run_a0_a1_reference_experiment_v1(replications=2)

    assert len(result.pairs) == 12 * 3 * 2
    assert result.summary.a0_saturated_worlds > 0
    assert result.summary.a1_adaptation_stabilized_worlds > 0
    assert result.summary.a1_saturated_worlds > 0
    assert result.summary.saturated_to_stable_worlds > 0
    assert result.summary.mean_adaptation_actor_time > 0.0
    assert result.summary.mean_adaptation_cost > 0.0


def test_long_horizon_adaptation_time_is_derived_from_actor_ledger() -> None:
    result = run_a0_a1_reference_experiment_v1(replications=2)
    longest = max(
        (pair.a1_run for pair in result.pairs),
        key=lambda run: run.adaptation_count,
    )

    recorded = longest.actor_ledger.duration_by_category().get(
        ActorCategory.ADAPTATION,
        0.0,
    )
    assert longest.adaptation_actor_time == recorded
