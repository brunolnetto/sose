from __future__ import annotations

from functools import lru_cache

import pytest

from sose.organizational.agency import AgencyLevel
from sose.organizational.experiment import ReplicationPlan
from sose.organizational.ledger import ActorCategory
from sose.organizational.synthetic_a1_experiment import (
    A1RegimeKind,
    build_a0_a1_reference_design_v1,
    classify_a1_regime,
    run_a0_a1_experiment,
)


@lru_cache(maxsize=1)
def _small_paired_result():
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
    small_design = design.model_copy(
        update={"protocol": protocol, "worlds": worlds}
    )
    return run_a0_a1_experiment(design=small_design)


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
    result = _small_paired_result()
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
    result = _small_paired_result()

    assert len(result.pairs) == 12 * 3 * 2
    assert result.summary.a0_saturated_worlds > 0
    assert result.summary.a1_adaptation_stabilized_worlds > 0
    assert result.summary.a1_saturated_worlds > 0
    assert result.summary.saturated_to_stable_worlds > 0
    assert result.summary.mean_adaptation_actor_time > 0.0
    assert result.summary.mean_adaptation_cost > 0.0


def test_long_horizon_adaptation_time_is_derived_from_actor_ledger() -> None:
    result = _small_paired_result()
    longest = max(
        (pair.a1_run for pair in result.pairs),
        key=lambda run: run.adaptation_count,
    )

    recorded = longest.actor_ledger.duration_by_category().get(
        ActorCategory.ADAPTATION,
        0.0,
    )
    assert longest.adaptation_actor_time == recorded


def test_runner_rejects_incomplete_or_duplicate_world_design() -> None:
    design = build_a0_a1_reference_design_v1()

    incomplete = design.model_copy(update={"worlds": design.worlds[:-1]})
    with pytest.raises(ValueError, match="complete A0/A1 world design"):
        run_a0_a1_experiment(design=incomplete)

    duplicate = design.model_copy(update={"worlds": (*design.worlds, design.worlds[-1])})
    with pytest.raises(ValueError, match="complete A0/A1 world design"):
        run_a0_a1_experiment(design=duplicate)


def test_runner_rejects_non_agency_mechanical_differences_inside_pair() -> None:
    design = build_a0_a1_reference_design_v1()
    target = next(
        world for world in design.worlds
        if world.design_index == 0
        and world.arm_id == "baseline"
        and world.agency_level is AgencyLevel.A1
    )
    payload = target.model_spec.canonical_payload()
    payload["parameters"]["service_capacity"] = (
        float(payload["parameters"]["service_capacity"]) * 1.1
    )
    changed_spec = target.model_spec.__class__.model_validate(payload)
    changed_world = target.model_copy(
        update={
            "model_spec": changed_spec,
            "model_spec_hash": changed_spec.model_spec_hash,
        }
    )
    worlds = tuple(
        changed_world if world is target else world
        for world in design.worlds
    )

    with pytest.raises(ValueError, match="differ only by agency"):
        run_a0_a1_experiment(design=design.model_copy(update={"worlds": worlds}))
