from __future__ import annotations

from sose.organizational.agency import AgencyLevel
from sose.organizational.synthetic_analysis import analytical_world_reference
from sose.organizational.synthetic_reference_experiment import (
    REFERENCE_DESIGN_SEED_V1,
    REFERENCE_ROOT_SEED_V1,
    build_reference_synthetic_experiment_v1,
    run_reference_synthetic_experiment_v1,
)


def test_reference_design_is_exogenous_hash_addressed_and_crosses_regimes() -> None:
    design = build_reference_synthetic_experiment_v1()

    assert design.design_seed == REFERENCE_DESIGN_SEED_V1
    assert design.root_seed == REFERENCE_ROOT_SEED_V1
    assert design.protocol.sample_size == 12
    assert design.protocol.replication_plan.min_replications == 16
    assert design.protocol.replication_plan.max_replications == 16
    assert design.protocol.agency_levels == (AgencyLevel.A0,)
    assert len(design.worlds) == 12 * 3

    references = tuple(analytical_world_reference(world) for world in design.worlds)
    assert any(reference.stable for reference in references)
    assert any(not reference.stable for reference in references)

    allowed_axes = {
        "arrival_rate",
        "service_capacity",
        "service_cv",
        "rework_probability",
        "transition_cost",
    }
    assert set(design.protocol.parameter_ranges) == allowed_axes
    assert all(set(world.exogenous_parameters) == allowed_axes for world in design.worlds)


def test_reference_interventions_are_stationary_and_mechanistically_distinct() -> None:
    design = build_reference_synthetic_experiment_v1()

    by_arm = {}
    for world in design.worlds:
        by_arm.setdefault(world.arm_id, []).append(world)

    assert set(by_arm) == {"baseline", "capacity-up", "automation-rework"}
    assert all(world.intervention_transition_time == 0.0 for world in design.worlds)

    for baseline, capacity, automation in zip(
        by_arm["baseline"],
        by_arm["capacity-up"],
        by_arm["automation-rework"],
        strict=True,
    ):
        assert capacity.model_spec.parameters["service_capacity"] == 1.6
        assert (
            capacity.model_spec.parameters["rework_probability"]
            == baseline.model_spec.parameters["rework_probability"]
        )
        assert automation.model_spec.parameters["rework_probability"] == 0.02
        assert (
            automation.model_spec.parameters["rework_probability"]
            < baseline.model_spec.parameters["rework_probability"]
        )
        assert (
            automation.model_spec.parameters["service_capacity"]
            == baseline.model_spec.parameters["service_capacity"]
        )


def test_reference_experiment_runs_and_binds_report_to_dataset() -> None:
    result = run_reference_synthetic_experiment_v1()

    assert result.dataset.replications == 16
    assert len(result.dataset.runs) == len(result.design.worlds) * 16
    assert result.report.protocol_hash == result.design.protocol.protocol_hash
    assert result.report.dataset_hash == result.dataset.dataset_hash
    assert result.report.world_count == len(result.design.worlds)
    assert result.report.stable_world_count > 0
    assert result.report.saturated_world_count > 0
    assert 0.0 <= result.report.direction_recovery_rate <= 1.0
    assert result.report_hash == result.report.report_hash
