from __future__ import annotations

from sose.organizational.agency import AgencyLevel, AgencySpec
from sose.organizational.experiment import (
    CostAnalysisPlan,
    ExperimentProtocol,
    FalsificationRule,
    MetricDirection,
    MultipleComparisonMethod,
    OutcomeMetric,
    ParameterRange,
    ReplicationPlan,
    SamplingDesign,
    StatisticalPlan,
    SurrogateAnalysisPlan,
    SurrogateModel,
)
from sose.organizational.model_spec import (
    EvidenceClass,
    InterventionClass,
    ModelIntervention,
    ModelSpec,
)
from sose.organizational.synthetic_analysis import (
    RegimeTransition,
    analytical_world_reference,
    analyze_reference_synthetic_experiment,
)
from sose.organizational.synthetic_runner import run_reference_synthetic_experiment
from sose.organizational.synthetic_study import SyntheticWorldSpec, generate_synthetic_worlds


def _single_world(
    *,
    arrival_rate: float,
    service_capacity: float,
    service_cv: float,
    rework_probability: float,
) -> SyntheticWorldSpec:
    spec = ModelSpec(
        parameters={
            "arrival_rate": arrival_rate,
            "service_capacity": service_capacity,
            "service_cv": service_cv,
            "rework_probability": rework_probability,
            "transition_cost": 0.0,
        },
        parameter_evidence={
            name: EvidenceClass.ASSUMED
            for name in (
                "arrival_rate",
                "service_capacity",
                "service_cv",
                "rework_probability",
                "transition_cost",
            )
        },
        costs={"operating_cost": 1.0},
    )
    return SyntheticWorldSpec(
        design_index=0,
        arm_id="baseline",
        agency_level=AgencyLevel.A0,
        crn_group="design:0:agency:A0",
        protocol_hash="a" * 64,
        baseline_model_spec_hash=spec.model_spec_hash,
        exogenous_parameters=dict(spec.parameters),
        model_spec=spec,
        model_spec_hash=spec.model_spec_hash,
    )


def test_analytical_reference_reduces_to_mm1_mean_lead_time() -> None:
    world = _single_world(
        arrival_rate=0.5,
        service_capacity=1.0,
        service_cv=1.0,
        rework_probability=0.0,
    )

    reference = analytical_world_reference(world)

    assert reference.stable is True
    assert reference.offered_load == 0.5
    assert reference.expected_mean_lead_time == 2.0
    assert reference.expected_throughput == 0.5


def test_analytical_reference_marks_saturated_world_without_finite_stationary_lead_time() -> None:
    world = _single_world(
        arrival_rate=1.1,
        service_capacity=1.0,
        service_cv=1.0,
        rework_probability=0.0,
    )

    reference = analytical_world_reference(world)

    assert reference.stable is False
    assert reference.offered_load == 1.1
    assert reference.expected_mean_lead_time is None
    assert reference.expected_throughput == 1.0


def _baseline() -> ModelSpec:
    return ModelSpec(
        parameters={
            "arrival_rate": 0.55,
            "service_capacity": 1.0,
            "service_cv": 0.8,
            "rework_probability": 0.12,
            "transition_cost": 0.0,
        },
        parameter_evidence={
            name: EvidenceClass.ASSUMED
            for name in (
                "arrival_rate",
                "service_capacity",
                "service_cv",
                "rework_probability",
                "transition_cost",
            )
        },
        costs={"operating_cost": 1.0},
    )


def _protocol(baseline: ModelSpec) -> ExperimentProtocol:
    return ExperimentProtocol(
        protocol_version="2",
        research_question="Recover known synthetic effects and regime boundaries.",
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("capacity-up", "automation"),
        agency_levels=(AgencyLevel.A0,),
        parameter_ranges={
            "arrival_rate": ParameterRange(low=0.45, high=0.65),
            "service_capacity": ParameterRange(low=0.9, high=1.1),
            "service_cv": ParameterRange(low=0.5, high=1.0),
            "rework_probability": ParameterRange(low=0.08, high=0.18),
            "transition_cost": ParameterRange(low=0.0, high=5.0),
        },
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=2,
        outcomes=(
            OutcomeMetric(
                name="lead_time",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=0.05,
            ),
            OutcomeMetric(
                name="operating_cost",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=1.0,
            ),
        ),
        statistical_plan=StatisticalPlan(
            confidence_level=0.95,
            multiple_comparison=MultipleComparisonMethod.HOLM,
        ),
        replication_plan=ReplicationPlan(
            min_replications=2,
            max_replications=4,
            target_ci_half_width=0.1,
        ),
        cost_analysis=CostAnalysisPlan(
            operating_cost_metric="operating_cost",
            transition_cost_parameter="transition_cost",
        ),
        surrogate_analysis=SurrogateAnalysisPlan(model=SurrogateModel.LINEAR),
        warmup=50.0,
        horizon=500.0,
        falsification=FalsificationRule(
            primary_metric="lead_time",
            theta_fraction=0.5,
        ),
    )


def _worlds_and_dataset():
    baseline = _baseline()
    protocol = _protocol(baseline)
    worlds = generate_synthetic_worlds(
        protocol=protocol,
        baseline=baseline,
        interventions=(
            ModelIntervention(
                intervention_id="capacity-up",
                intervention_class=InterventionClass.CAPACITY,
                set_values={"/parameters/service_capacity": 1.5},
            ),
            ModelIntervention(
                intervention_id="automation",
                intervention_class=InterventionClass.AUTOMATION,
                set_values={"/parameters/rework_probability": 0.02},
            ),
        ),
        agency_specs={AgencyLevel.A0: AgencySpec(level=AgencyLevel.A0)},
        seed=11,
    )
    dataset = run_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        root_seed=20261007,
        replications=4,
    )
    return protocol, worlds, dataset


def test_recovery_report_uses_exogenous_mechanical_regimes_and_paired_effects() -> None:
    protocol, worlds, dataset = _worlds_and_dataset()

    report = analyze_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        dataset=dataset,
    )

    assert report.protocol_hash == protocol.protocol_hash
    assert report.dataset_hash == dataset.dataset_hash
    assert report.world_count == len(worlds)
    assert report.stable_world_count == len(worlds)
    assert report.saturated_world_count == 0
    assert report.mean_relative_error_stable >= 0.0
    assert report.effects
    assert all(effect.arm_id != "baseline" for effect in report.effects)
    assert all(effect.expected_delta_mean_lead_time is not None for effect in report.effects)
    assert all(effect.simulated_delta_mean_lead_time is not None for effect in report.effects)
    assert all(effect.regime_transition is RegimeTransition.STABLE_TO_STABLE for effect in report.effects)
    assert all(effect.simulated_delta_operating_cost >= 0.0 for effect in report.effects)

    capacity_effects = [effect for effect in report.effects if effect.arm_id == "capacity-up"]
    automation_effects = [effect for effect in report.effects if effect.arm_id == "automation"]
    assert all(effect.expected_delta_mean_lead_time < 0.0 for effect in capacity_effects)
    assert all(effect.expected_delta_mean_lead_time < 0.0 for effect in automation_effects)


def test_recovery_report_is_hash_addressed_and_rejects_incomplete_world_binding() -> None:
    protocol, worlds, dataset = _worlds_and_dataset()
    left = analyze_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        dataset=dataset,
    )
    right = analyze_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        dataset=dataset,
    )

    assert left == right
    assert left.report_hash == right.report_hash

    try:
        analyze_reference_synthetic_experiment(
            protocol=protocol,
            worlds=worlds[:-1],
            dataset=dataset,
        )
    except ValueError as exc:
        assert "world binding" in str(exc)
    else:
        raise AssertionError("expected incomplete world-binding rejection")
