from __future__ import annotations

import pytest
from pydantic import ValidationError

from sose.organizational.agency import AgencyLevel
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


def _v1_payload() -> dict[str, object]:
    return {
        "protocol_version": "1",
        "research_question": "Which intervention class wins?",
        "baseline_model_spec_hash": "a" * 64,
        "intervention_ids": ("capacity", "structure"),
        "agency_levels": (AgencyLevel.A0,),
        "parameter_ranges": {"arrival_cv": ParameterRange(low=0.5, high=2.0)},
        "sampling_design": SamplingDesign.LATIN_HYPERCUBE,
        "sample_size": 16,
        "outcomes": (
            OutcomeMetric(
                name="lead_time",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=0.1,
            ),
        ),
        "statistical_plan": StatisticalPlan(
            confidence_level=0.95,
            multiple_comparison=MultipleComparisonMethod.HOLM,
        ),
        "replication_plan": ReplicationPlan(
            min_replications=2,
            max_replications=10,
            target_ci_half_width=0.1,
        ),
        "warmup": 1.0,
        "horizon": 10.0,
        "falsification": FalsificationRule(
            primary_metric="lead_time",
            theta_fraction=0.5,
        ),
    }


def test_protocol_rejects_unknown_versions() -> None:
    payload = _v1_payload()
    payload["protocol_version"] = "20"

    with pytest.raises(ValidationError):
        ExperimentProtocol.model_validate(payload)


def test_protocol_v1_rejects_v2_only_analysis_plans() -> None:
    payload = _v1_payload()
    payload["cost_analysis"] = CostAnalysisPlan(
        operating_cost_metric="lead_time",
        transition_cost_parameter="arrival_cv",
    )
    payload["surrogate_analysis"] = SurrogateAnalysisPlan(
        model=SurrogateModel.LINEAR,
    )

    with pytest.raises(ValidationError, match="version 1"):
        ExperimentProtocol.model_validate(payload)
