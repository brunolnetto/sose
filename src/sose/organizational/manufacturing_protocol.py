from __future__ import annotations

from .agency import AgencyLevel
from .domain_experiment import AgencyConfiguration
from .domain_experiment_runtime import DomainExperimentPlan
from .experiment import (
    ExperimentProtocol,
    FalsificationRule,
    MetricDirection,
    MultipleComparisonMethod,
    OutcomeMetric,
    ParameterRange,
    ReplicationPlan,
    SamplingDesign,
    StatisticalPlan,
)
from .manufacturing_reference import ManufacturingReferenceDomain


MANUFACTURING_V1_DESIGN_SEED = 20261008
MANUFACTURING_V1_ROOT_SEED = 20261008


def build_manufacturing_protocol_v1(
    reference: ManufacturingReferenceDomain | None = None,
) -> ExperimentProtocol:
    """Return the preregistered executable protocol for Manufacturing v1.

    This function is configuration-only. It must not execute any experiment world
    or inspect any official result.
    """

    reference = reference or ManufacturingReferenceDomain()
    baseline = reference.build_model({"quantity": 10.0})
    return ExperimentProtocol(
        protocol_version="1",
        research_question=(
            "Can the SOSE Manufacturing PC5 canonical preserve preregistered "
            "durable invariants and recover the mechanistic effects of finite "
            "machine-downtime and yield-degradation interventions under "
            "deterministic replicated A0 execution?"
        ),
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("machine_downtime", "yield_degradation"),
        agency_levels=(AgencyLevel.A0,),
        parameter_ranges={
            "quantity": ParameterRange(low=1.0, high=1000.0),
        },
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=6,
        outcomes=(
            OutcomeMetric(
                name="completed",
                direction=MetricDirection.MAXIMIZE,
                equivalence_margin=1e-9,
            ),
            OutcomeMetric(
                name="lead_time_seconds",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=1.0,
            ),
            OutcomeMetric(
                name="output_quantity",
                direction=MetricDirection.MAXIMIZE,
                equivalence_margin=1e-9,
            ),
            OutcomeMetric(
                name="yield_ratio",
                direction=MetricDirection.MAXIMIZE,
                equivalence_margin=1e-9,
            ),
            OutcomeMetric(
                name="transition_count",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=1e-9,
            ),
            OutcomeMetric(
                name="rework_count",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=1e-9,
            ),
            OutcomeMetric(
                name="breakdown_count",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=1e-9,
            ),
        ),
        statistical_plan=StatisticalPlan(
            confidence_level=0.95,
            multiple_comparison=MultipleComparisonMethod.HOLM,
        ),
        replication_plan=ReplicationPlan(
            min_replications=2,
            max_replications=2,
            target_ci_half_width=0.01,
        ),
        warmup=0.0,
        horizon=24.0,
        falsification=FalsificationRule(
            primary_metric="output_quantity",
            theta_fraction=0.5,
        ),
        crn_enabled=True,
        report_null_regions=True,
    )


def build_manufacturing_official_plan_v1(
    reference: ManufacturingReferenceDomain | None = None,
) -> DomainExperimentPlan:
    """Return the frozen configuration inputs for the later official v1 execution."""

    reference = reference or ManufacturingReferenceDomain()
    return DomainExperimentPlan(
        protocol=build_manufacturing_protocol_v1(reference),
        design_seed=MANUFACTURING_V1_DESIGN_SEED,
        root_seed=MANUFACTURING_V1_ROOT_SEED,
        agency_configurations=(
            AgencyConfiguration(
                level=AgencyLevel.A0,
                capability_id="fixed",
            ),
        ),
    )
