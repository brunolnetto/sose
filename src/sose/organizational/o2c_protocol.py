"""O2C v1 executable preregistration, configuration only: never executes worlds."""
from __future__ import annotations

from .agency import AgencyLevel
from .domain_experiment import AgencyConfiguration
from .domain_experiment_runtime import DomainExperimentPlan
from .experiment import (
    ExperimentProtocol, FalsificationRule, MetricDirection,
    MultipleComparisonMethod, OutcomeMetric, ParameterRange,
    ReplicationPlan, SamplingDesign, StatisticalPlan,
)
from .o2c_reference import O2CReferenceDomain

DESIGN_SEED = 20261008
ROOT_SEED = 20261008


def build_o2c_protocol_v1(reference: O2CReferenceDomain | None = None) -> ExperimentProtocol:
    """Return prospective protocol; no result inspection or post-hoc tuning."""
    reference = reference or O2CReferenceDomain()
    baseline = reference.build_model({"due_delay_hours": 2.0})
    return ExperimentProtocol(
        protocol_version="1",
        research_question="Do fixed-policy administrative handoffs, partial fulfillment and overdue collection preserve durable O2C invariants and recover configured mechanisms under paired synthetic runs?",
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("partial_fulfillment","overdue_collection",),
        agency_levels=(AgencyLevel.A0,),
        parameter_ranges={"due_delay_hours": ParameterRange(low=1.0, high=5.0)},
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=4,
        outcomes=(
            OutcomeMetric(name="collected", direction=MetricDirection.MAXIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="order_to_cash_seconds", direction=MetricDirection.MINIMIZE, equivalence_margin=1),
            OutcomeMetric(name="partial_fulfillment_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="overdue_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="collection_case_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="collection_escalation_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="amount", direction=MetricDirection.MAXIMIZE, equivalence_margin=1e-9),
        ),
        statistical_plan=StatisticalPlan(confidence_level=0.95, multiple_comparison=MultipleComparisonMethod.HOLM),
        replication_plan=ReplicationPlan(min_replications=2, max_replications=2, target_ci_half_width=0.01),
        warmup=0.0,
        horizon=24.0,
        falsification=FalsificationRule(primary_metric="order_to_cash_seconds", theta_fraction=0.5),
        crn_enabled=True,
        report_null_regions=True,
    )


def build_o2c_official_plan_v1(reference: O2CReferenceDomain | None = None) -> DomainExperimentPlan:
    reference = reference or O2CReferenceDomain()
    return DomainExperimentPlan(
        protocol=build_o2c_protocol_v1(reference),
        design_seed=DESIGN_SEED,
        root_seed=ROOT_SEED,
        agency_configurations=(AgencyConfiguration(level=AgencyLevel.A0, capability_id="fixed"),),
    )
