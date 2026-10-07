from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .agency import AgencyLevel, AgencySpec
from .experiment import (
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
from .model_spec import (
    EvidenceClass,
    InterventionClass,
    ModelIntervention,
    ModelSpec,
)
from .synthetic_analysis import (
    SyntheticRecoveryReport,
    analyze_reference_synthetic_experiment,
)
from .synthetic_runner import (
    SyntheticExperimentDataset,
    run_reference_synthetic_experiment,
)
from .synthetic_study import SyntheticWorldSpec, generate_synthetic_worlds


CANONICAL_DESIGN_SEED = 20261007
CANONICAL_ROOT_SEED = 20261008


class SyntheticVerificationCriteria(BaseModel):
    """Frozen pass/fail criteria for the reference synthetic verification run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_mean_relative_error_stable: float = Field(gt=0.0, allow_inf_nan=False)
    min_direction_recovery_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    require_stable_worlds: bool = True
    require_saturated_worlds: bool = True


class SyntheticVerificationAssessment(BaseModel):
    """Separate verification gates; intentionally no composite score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stable_region_present: bool
    saturated_region_present: bool
    stable_mean_error_passed: bool
    effect_direction_passed: bool

    @property
    def passed(self) -> bool:
        return all(
            (
                self.stable_region_present,
                self.saturated_region_present,
                self.stable_mean_error_passed,
                self.effect_direction_passed,
            )
        )


def canonical_verification_criteria() -> SyntheticVerificationCriteria:
    return SyntheticVerificationCriteria(
        max_mean_relative_error_stable=0.25,
        min_direction_recovery_rate=0.85,
        require_stable_worlds=True,
        require_saturated_worlds=True,
    )


def build_reference_synthetic_canonical() -> tuple[
    ModelSpec,
    ExperimentProtocol,
    tuple[ModelIntervention, ...],
    dict[AgencyLevel, AgencySpec],
]:
    """Build the preregistered A0 synthetic canonical without empirical inputs."""

    baseline = ModelSpec(
        demand={"kind": "synthetic_open_system"},
        costs={"operating_cost": 1.0},
        parameters={
            "arrival_rate": 1.0,
            "service_capacity": 1.0,
            "service_cv": 1.0,
            "rework_probability": 0.10,
            "transition_cost": 0.0,
        },
        parameter_evidence={
            "arrival_rate": EvidenceClass.ASSUMED,
            "service_capacity": EvidenceClass.ASSUMED,
            "service_cv": EvidenceClass.ASSUMED,
            "rework_probability": EvidenceClass.ASSUMED,
            "transition_cost": EvidenceClass.ASSUMED,
        },
    )

    interventions = (
        ModelIntervention(
            intervention_id="capacity-up",
            intervention_class=InterventionClass.CAPACITY,
            set_values={"/parameters/service_capacity": 1.8},
            operating_cost=0.45,
            transition_cost=12.0,
            transition_time=40.0,
        ),
        ModelIntervention(
            intervention_id="policy-standardize",
            intervention_class=InterventionClass.POLICY,
            set_values={"/parameters/service_cv": 0.35},
            operating_cost=0.10,
            transition_cost=5.0,
            transition_time=10.0,
        ),
        ModelIntervention(
            intervention_id="automation",
            intervention_class=InterventionClass.AUTOMATION,
            set_values={"/parameters/rework_probability": 0.01},
            operating_cost=0.25,
            transition_cost=18.0,
            transition_time=30.0,
        ),
        ModelIntervention(
            intervention_id="structural-cell",
            intervention_class=InterventionClass.STRUCTURAL,
            set_values={
                "/parameters/service_capacity": 1.6,
                "/parameters/service_cv": 0.50,
                "/parameters/rework_probability": 0.04,
            },
            operating_cost=0.65,
            transition_cost=30.0,
            transition_time=60.0,
        ),
    )

    protocol = ExperimentProtocol(
        protocol_version="2",
        research_question=(
            "Across exogenous load, capacity, variability, rework, and transition-cost "
            "regimes, when do capacity, policy, automation, and structural interventions "
            "change lead time, throughput, WIP, and cost in the known A0 synthetic process?"
        ),
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=tuple(item.intervention_id for item in interventions),
        agency_levels=(AgencyLevel.A0,),
        parameter_ranges={
            "arrival_rate": ParameterRange(low=0.2, high=2.0),
            "service_capacity": ParameterRange(low=0.8, high=1.4),
            "service_cv": ParameterRange(low=0.3, high=1.8),
            "rework_probability": ParameterRange(low=0.0, high=0.35),
            "transition_cost": ParameterRange(low=0.0, high=25.0),
        },
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=24,
        outcomes=(
            OutcomeMetric(
                name="lead_time",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=0.10,
            ),
            OutcomeMetric(
                name="throughput",
                direction=MetricDirection.MAXIMIZE,
                equivalence_margin=0.02,
            ),
            OutcomeMetric(
                name="mean_wip",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=0.10,
            ),
            OutcomeMetric(
                name="operating_cost",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=5.0,
            ),
        ),
        statistical_plan=StatisticalPlan(
            confidence_level=0.95,
            multiple_comparison=MultipleComparisonMethod.HOLM,
        ),
        replication_plan=ReplicationPlan(
            min_replications=4,
            max_replications=4,
            target_ci_half_width=0.10,
        ),
        cost_analysis=CostAnalysisPlan(
            operating_cost_metric="operating_cost",
            transition_cost_parameter="transition_cost",
        ),
        surrogate_analysis=SurrogateAnalysisPlan(
            model=SurrogateModel.GENERALIZED_ADDITIVE,
            global_sensitivity=True,
            ablations=True,
        ),
        warmup=250.0,
        horizon=1000.0,
        falsification=FalsificationRule(
            primary_metric="lead_time",
            theta_fraction=0.5,
        ),
        crn_enabled=True,
        report_null_regions=True,
    )

    return (
        baseline,
        protocol,
        interventions,
        {AgencyLevel.A0: AgencySpec(level=AgencyLevel.A0)},
    )


def assess_reference_synthetic_recovery(
    report: SyntheticRecoveryReport,
    *,
    criteria: SyntheticVerificationCriteria | None = None,
) -> SyntheticVerificationAssessment:
    resolved = canonical_verification_criteria() if criteria is None else criteria
    return SyntheticVerificationAssessment(
        stable_region_present=(
            report.stable_world_count > 0 if resolved.require_stable_worlds else True
        ),
        saturated_region_present=(
            report.saturated_world_count > 0 if resolved.require_saturated_worlds else True
        ),
        stable_mean_error_passed=(
            report.mean_relative_error_stable
            <= resolved.max_mean_relative_error_stable
        ),
        effect_direction_passed=(
            report.direction_recovery_rate
            >= resolved.min_direction_recovery_rate
        ),
    )


def run_reference_synthetic_canonical() -> tuple[
    tuple[SyntheticWorldSpec, ...],
    SyntheticExperimentDataset,
    SyntheticRecoveryReport,
    SyntheticVerificationAssessment,
]:
    baseline, protocol, interventions, agency_specs = build_reference_synthetic_canonical()
    worlds = generate_synthetic_worlds(
        protocol=protocol,
        baseline=baseline,
        interventions=interventions,
        agency_specs=agency_specs,
        seed=CANONICAL_DESIGN_SEED,
    )
    dataset = run_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        root_seed=CANONICAL_ROOT_SEED,
        replications=protocol.replication_plan.min_replications,
    )
    report = analyze_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        dataset=dataset,
    )
    assessment = assess_reference_synthetic_recovery(report)
    return worlds, dataset, report, assessment
