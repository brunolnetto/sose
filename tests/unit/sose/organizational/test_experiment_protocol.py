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


BASELINE_HASH = "a" * 64


def _base_payload() -> dict[str, object]:
    return {
        "research_question": "Which intervention class wins under preregistered operating regimes?",
        "baseline_model_spec_hash": BASELINE_HASH,
        "intervention_ids": ("capacity-reviewer", "delegate-low-risk"),
        "agency_levels": (AgencyLevel.A0, AgencyLevel.A1, AgencyLevel.A2),
        "sampling_design": SamplingDesign.LATIN_HYPERCUBE,
        "sample_size": 128,
        "statistical_plan": StatisticalPlan(
            confidence_level=0.95,
            multiple_comparison=MultipleComparisonMethod.HOLM,
        ),
        "replication_plan": ReplicationPlan(
            min_replications=20,
            max_replications=100,
            target_ci_half_width=0.05,
        ),
        "warmup": 100.0,
        "horizon": 1000.0,
        "falsification": FalsificationRule(
            primary_metric="lead_time",
            theta_fraction=0.5,
        ),
        "crn_enabled": True,
        "report_null_regions": True,
    }


def _protocol(**overrides: object) -> ExperimentProtocol:
    payload = _base_payload()
    payload.update(
        {
            "protocol_version": "2",
            "parameter_ranges": {
                "arrival_cv": ParameterRange(low=0.5, high=2.0),
                "service_cv": ParameterRange(low=0.5, high=2.5),
                "transition_cost": ParameterRange(low=0.0, high=100.0),
            },
            "outcomes": (
                OutcomeMetric(
                    name="lead_time",
                    direction=MetricDirection.MINIMIZE,
                    equivalence_margin=0.1,
                ),
                OutcomeMetric(
                    name="defect_escape",
                    direction=MetricDirection.MINIMIZE,
                    equivalence_margin=0.01,
                ),
                OutcomeMetric(
                    name="operating_cost",
                    direction=MetricDirection.MINIMIZE,
                    equivalence_margin=1.0,
                ),
            ),
            "cost_analysis": CostAnalysisPlan(
                operating_cost_metric="operating_cost",
                transition_cost_parameter="transition_cost",
                break_even_analysis=True,
            ),
            "surrogate_analysis": SurrogateAnalysisPlan(
                model=SurrogateModel.GENERALIZED_ADDITIVE,
                global_sensitivity=True,
                ablations=True,
            ),
        }
    )
    payload.update(overrides)
    return ExperimentProtocol.model_validate(payload)


def test_protocol_is_canonical_and_hash_addressed() -> None:
    left = _protocol()
    right = _protocol(
        parameter_ranges={
            "transition_cost": ParameterRange(low=0.0, high=100.0),
            "service_cv": ParameterRange(low=0.5, high=2.5),
            "arrival_cv": ParameterRange(low=0.5, high=2.0),
        }
    )

    assert left.canonical_json() == right.canonical_json()
    assert left.protocol_hash == right.protocol_hash
    assert len(left.protocol_hash) == 64


def test_protocol_hash_changes_when_preregistered_space_changes() -> None:
    baseline = _protocol()
    changed = _protocol(
        parameter_ranges={
            "arrival_cv": ParameterRange(low=0.5, high=3.0),
            "service_cv": ParameterRange(low=0.5, high=2.5),
            "transition_cost": ParameterRange(low=0.0, high=100.0),
        }
    )
    assert baseline.protocol_hash != changed.protocol_hash


def test_parameter_range_requires_finite_strict_bounds() -> None:
    with pytest.raises(ValidationError):
        ParameterRange(low=1.0, high=1.0)
    with pytest.raises(ValidationError):
        ParameterRange(low=2.0, high=1.0)
    with pytest.raises(ValidationError):
        ParameterRange(low=0.0, high=float("inf"))


def test_protocol_rejects_a3_and_duplicate_preregistered_choices() -> None:
    with pytest.raises(ValidationError, match="A3"):
        _protocol(agency_levels=(AgencyLevel.A0, AgencyLevel.A3))
    with pytest.raises(ValidationError, match="duplicate intervention"):
        _protocol(intervention_ids=("capacity-reviewer", "capacity-reviewer"))
    with pytest.raises(ValidationError, match="duplicate agency"):
        _protocol(agency_levels=(AgencyLevel.A0, AgencyLevel.A0))


def test_protocol_requires_exogenous_space_outcomes_and_null_region_reporting() -> None:
    with pytest.raises(ValidationError, match="parameter range"):
        _protocol(parameter_ranges={})
    with pytest.raises(ValidationError, match="outcome"):
        _protocol(outcomes=())
    with pytest.raises(ValidationError, match="null regions"):
        _protocol(report_null_regions=False)


def test_primary_falsification_metric_must_be_preregistered() -> None:
    with pytest.raises(ValidationError, match="primary falsification metric"):
        _protocol(
            falsification=FalsificationRule(
                primary_metric="throughput",
                theta_fraction=0.5,
            )
        )


def test_comparative_protocol_requires_crn() -> None:
    with pytest.raises(ValidationError, match="CRN"):
        _protocol(crn_enabled=False)


def test_outcomes_require_positive_material_equivalence_margin_and_unique_names() -> None:
    with pytest.raises(ValidationError):
        OutcomeMetric(
            name="lead_time",
            direction=MetricDirection.MINIMIZE,
            equivalence_margin=0.0,
        )

    duplicate = OutcomeMetric(
        name="lead_time",
        direction=MetricDirection.MINIMIZE,
        equivalence_margin=0.1,
    )
    with pytest.raises(ValidationError, match="duplicate outcome"):
        _protocol(outcomes=(duplicate, duplicate))


def test_replication_and_statistical_contracts_are_bounded() -> None:
    with pytest.raises(ValidationError):
        ReplicationPlan(min_replications=10, max_replications=5, target_ci_half_width=0.1)
    with pytest.raises(ValidationError):
        ReplicationPlan(min_replications=2, max_replications=5, target_ci_half_width=0.0)
    with pytest.raises(ValidationError):
        StatisticalPlan(confidence_level=1.0, multiple_comparison=MultipleComparisonMethod.HOLM)


def test_sampling_design_is_space_filling_not_full_grid() -> None:
    assert {design.value for design in SamplingDesign} == {"latin_hypercube", "sobol"}


def test_cost_plan_must_reference_preregistered_metric_and_parameter() -> None:
    with pytest.raises(ValidationError, match="operating cost metric"):
        _protocol(
            cost_analysis=CostAnalysisPlan(
                operating_cost_metric="missing_cost",
                transition_cost_parameter="transition_cost",
            )
        )
    with pytest.raises(ValidationError, match="transition cost parameter"):
        _protocol(
            cost_analysis=CostAnalysisPlan(
                operating_cost_metric="operating_cost",
                transition_cost_parameter="missing_transition_cost",
            )
        )


def test_phase6_requires_break_even_and_interpretable_surrogate_analysis() -> None:
    with pytest.raises(ValidationError, match="break-even"):
        _protocol(
            cost_analysis=CostAnalysisPlan(
                operating_cost_metric="operating_cost",
                transition_cost_parameter="transition_cost",
                break_even_analysis=False,
            )
        )
    with pytest.raises(ValidationError, match="global sensitivity"):
        _protocol(
            surrogate_analysis=SurrogateAnalysisPlan(
                model=SurrogateModel.LINEAR,
                global_sensitivity=False,
                ablations=True,
            )
        )
    with pytest.raises(ValidationError, match="ablations"):
        _protocol(
            surrogate_analysis=SurrogateAnalysisPlan(
                model=SurrogateModel.SHALLOW_TREE,
                global_sensitivity=True,
                ablations=False,
            )
        )


def test_version_1_protocol_remains_hash_compatible_without_new_phase6_fields() -> None:
    payload = _base_payload()
    payload.update(
        {
            "protocol_version": "1",
            "parameter_ranges": {
                "arrival_cv": ParameterRange(low=0.5, high=2.0),
                "service_cv": ParameterRange(low=0.5, high=2.5),
            },
            "outcomes": (
                OutcomeMetric(
                    name="lead_time",
                    direction=MetricDirection.MINIMIZE,
                    equivalence_margin=0.1,
                ),
            ),
        }
    )
    protocol = ExperimentProtocol.model_validate(payload)
    canonical = protocol.canonical_payload()

    assert "cost_analysis" not in canonical
    assert "surrogate_analysis" not in canonical


def test_version_2_requires_cost_and_surrogate_analysis() -> None:
    with pytest.raises(ValidationError, match="cost analysis"):
        _protocol(cost_analysis=None)
    with pytest.raises(ValidationError, match="surrogate analysis"):
        _protocol(surrogate_analysis=None)


def test_canonical_payload_records_all_preregistration_decisions() -> None:
    protocol = _protocol()
    payload = protocol.canonical_payload()

    assert payload["baseline_model_spec_hash"] == BASELINE_HASH
    assert payload["sampling_design"] == "latin_hypercube"
    assert payload["agency_levels"] == ["A0", "A1", "A2"]
    assert payload["falsification"] == {
        "primary_metric": "lead_time",
        "theta_fraction": 0.5,
    }
    assert payload["statistical_plan"]["multiple_comparison"] == "holm"
    assert payload["cost_analysis"] == {
        "operating_cost_metric": "operating_cost",
        "transition_cost_parameter": "transition_cost",
        "break_even_analysis": True,
    }
    assert payload["surrogate_analysis"] == {
        "model": "generalized_additive",
        "global_sensitivity": True,
        "ablations": True,
    }
