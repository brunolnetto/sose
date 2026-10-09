"""MRO v1 executable preregistration, configuration only: never executes worlds."""
from __future__ import annotations

from .agency import AgencyLevel
from .domain_experiment import AgencyConfiguration
from .domain_experiment_runtime import DomainExperimentPlan
from .experiment import (
    ExperimentProtocol, FalsificationRule, MetricDirection,
    MultipleComparisonMethod, OutcomeMetric, ParameterRange,
    ReplicationPlan, SamplingDesign, StatisticalPlan,
)
from .mro_reference import MROReferenceDomain

DESIGN_SEED = 20261008
ROOT_SEED = 20261008


def build_mro_protocol_v1(reference: MROReferenceDomain | None = None) -> ExperimentProtocol:
    """Return prospective protocol; no result inspection or post-hoc tuning."""
    reference = reference or MROReferenceDomain()
    baseline = reference.build_model({"quantity": 2.0})
    return ExperimentProtocol(
        protocol_version="1",
        research_question="Do finite spare-part shortages and emergency preemption preserve MRO resource and inventory invariants and recover configured mechanistic effects in durable A0 synthetic runs?",
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("spare_part_shortage","emergency_preemption",),
        agency_levels=(AgencyLevel.A0,),
        parameter_ranges={"quantity": ParameterRange(low=1.0, high=20.0)},
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=6,
        outcomes=(
            OutcomeMetric(name="closed", direction=MetricDirection.MAXIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="lead_time_seconds", direction=MetricDirection.MINIMIZE, equivalence_margin=1),
            OutcomeMetric(name="parts_consumed", direction=MetricDirection.MAXIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="remaining_spare_parts", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="spare_part_issue_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="material_wait_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="resource_wait_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="interruption_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="preemption_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
        ),
        statistical_plan=StatisticalPlan(confidence_level=0.95, multiple_comparison=MultipleComparisonMethod.HOLM),
        replication_plan=ReplicationPlan(min_replications=2, max_replications=2, target_ci_half_width=0.01),
        warmup=0.0,
        horizon=24.0,
        falsification=FalsificationRule(primary_metric="lead_time_seconds", theta_fraction=0.5),
        crn_enabled=True,
        report_null_regions=True,
    )


def build_mro_official_plan_v1(reference: MROReferenceDomain | None = None) -> DomainExperimentPlan:
    reference = reference or MROReferenceDomain()
    return DomainExperimentPlan(
        protocol=build_mro_protocol_v1(reference),
        design_seed=DESIGN_SEED,
        root_seed=ROOT_SEED,
        agency_configurations=(AgencyConfiguration(level=AgencyLevel.A0, capability_id="fixed"),),
    )
