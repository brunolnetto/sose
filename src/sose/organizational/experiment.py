from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .agency import AgencyLevel


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SamplingDesign(StrEnum):
    LATIN_HYPERCUBE = "latin_hypercube"
    SOBOL = "sobol"


class MetricDirection(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class MultipleComparisonMethod(StrEnum):
    HOLM = "holm"
    FDR_BH = "fdr_bh"


class SurrogateModel(StrEnum):
    LINEAR = "linear"
    GENERALIZED_ADDITIVE = "generalized_additive"
    SHALLOW_TREE = "shallow_tree"


class ParameterRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    low: float = Field(allow_inf_nan=False)
    high: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_bounds(self) -> "ParameterRange":
        if not isfinite(self.low) or not isfinite(self.high) or self.low >= self.high:
            raise ValueError("parameter range requires finite low < high")
        return self

    def canonical_payload(self) -> dict[str, float]:
        return {"low": self.low, "high": self.high}


class OutcomeMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonBlankString
    direction: MetricDirection
    equivalence_margin: float = Field(gt=0.0, allow_inf_nan=False)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "direction": self.direction.value,
            "equivalence_margin": self.equivalence_margin,
        }


class StatisticalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    confidence_level: float = Field(gt=0.0, lt=1.0, allow_inf_nan=False)
    multiple_comparison: MultipleComparisonMethod

    def canonical_payload(self) -> dict[str, object]:
        return {
            "confidence_level": self.confidence_level,
            "multiple_comparison": self.multiple_comparison.value,
        }


class ReplicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_replications: int = Field(ge=2)
    max_replications: int = Field(ge=2)
    target_ci_half_width: float = Field(gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_replication_bounds(self) -> "ReplicationPlan":
        if self.max_replications < self.min_replications:
            raise ValueError("max_replications must be >= min_replications")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "min_replications": self.min_replications,
            "max_replications": self.max_replications,
            "target_ci_half_width": self.target_ci_half_width,
        }


class CostAnalysisPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operating_cost_metric: NonBlankString
    transition_cost_parameter: NonBlankString
    break_even_analysis: bool = True

    @model_validator(mode="after")
    def require_break_even_analysis(self) -> "CostAnalysisPlan":
        if not self.break_even_analysis:
            raise ValueError("Phase 6 requires explicit break-even analysis")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "operating_cost_metric": self.operating_cost_metric,
            "transition_cost_parameter": self.transition_cost_parameter,
            "break_even_analysis": self.break_even_analysis,
        }


class SurrogateAnalysisPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: SurrogateModel
    global_sensitivity: bool = True
    ablations: bool = True

    @model_validator(mode="after")
    def require_interpretable_analysis(self) -> "SurrogateAnalysisPlan":
        if not self.global_sensitivity:
            raise ValueError("Phase 6 surrogate analysis requires global sensitivity reporting")
        if not self.ablations:
            raise ValueError("Phase 6 surrogate analysis requires preregistered ablations")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "model": self.model.value,
            "global_sensitivity": self.global_sensitivity,
            "ablations": self.ablations,
        }


class FalsificationRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    primary_metric: NonBlankString
    theta_fraction: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "primary_metric": self.primary_metric,
            "theta_fraction": self.theta_fraction,
        }


class ExperimentProtocol(BaseModel):
    """Hash-addressed preregistration contract for organizational experiments."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    protocol_version: Literal["1", "2"] = "1"
    research_question: NonBlankString
    baseline_model_spec_hash: NonBlankString
    intervention_ids: tuple[NonBlankString, ...] = Field(min_length=2)
    agency_levels: tuple[AgencyLevel, ...] = Field(min_length=1)
    parameter_ranges: dict[str, ParameterRange] = Field(default_factory=dict)
    sampling_design: SamplingDesign
    sample_size: int = Field(ge=2)
    outcomes: tuple[OutcomeMetric, ...] = ()
    statistical_plan: StatisticalPlan
    replication_plan: ReplicationPlan
    cost_analysis: CostAnalysisPlan | None = None
    surrogate_analysis: SurrogateAnalysisPlan | None = None
    warmup: float = Field(ge=0.0, allow_inf_nan=False)
    horizon: float = Field(gt=0.0, allow_inf_nan=False)
    falsification: FalsificationRule
    crn_enabled: bool = True
    report_null_regions: bool = True

    @model_validator(mode="after")
    def validate_protocol(self) -> "ExperimentProtocol":
        if len(self.baseline_model_spec_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.baseline_model_spec_hash
        ):
            raise ValueError("baseline_model_spec_hash must be a lowercase SHA-256 hex digest")
        if len(set(self.intervention_ids)) != len(self.intervention_ids):
            raise ValueError("duplicate intervention ids are not allowed")
        if len(set(self.agency_levels)) != len(self.agency_levels):
            raise ValueError("duplicate agency levels are not allowed")
        if AgencyLevel.A3 in self.agency_levels:
            raise ValueError("A3 is outside the organizational simulation model")
        if not self.parameter_ranges:
            raise ValueError("at least one parameter range is required")
        if not self.outcomes:
            raise ValueError("at least one outcome is required")
        outcome_names = [outcome.name for outcome in self.outcomes]
        if len(set(outcome_names)) != len(outcome_names):
            raise ValueError("duplicate outcome names are not allowed")
        if self.falsification.primary_metric not in outcome_names:
            raise ValueError("primary falsification metric must be a preregistered outcome")
        if self.protocol_version == "1":
            if self.cost_analysis is not None or self.surrogate_analysis is not None:
                raise ValueError("protocol version 1 does not accept version 2 analysis plans")
        else:
            if self.cost_analysis is None:
                raise ValueError("protocol version 2 requires cost analysis")
            if self.surrogate_analysis is None:
                raise ValueError("protocol version 2 requires surrogate analysis")
            if self.cost_analysis.operating_cost_metric not in outcome_names:
                raise ValueError("operating cost metric must be a preregistered outcome")
            if self.cost_analysis.transition_cost_parameter not in self.parameter_ranges:
                raise ValueError("transition cost parameter must be a preregistered parameter range")
        if self.warmup >= self.horizon:
            raise ValueError("warmup must be smaller than horizon")
        if not self.crn_enabled:
            raise ValueError("comparative organizational protocols require CRN")
        if not self.report_null_regions:
            raise ValueError("preregistration must commit to reporting null regions")
        object.__setattr__(
            self,
            "parameter_ranges",
            MappingProxyType(dict(self.parameter_ranges)),
        )
        return self

    def canonical_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "research_question": self.research_question,
            "baseline_model_spec_hash": self.baseline_model_spec_hash,
            "intervention_ids": list(self.intervention_ids),
            "agency_levels": [level.value for level in self.agency_levels],
            "parameter_ranges": {
                name: parameter_range.canonical_payload()
                for name, parameter_range in self.parameter_ranges.items()
            },
            "sampling_design": self.sampling_design.value,
            "sample_size": self.sample_size,
            "outcomes": [outcome.canonical_payload() for outcome in self.outcomes],
            "statistical_plan": self.statistical_plan.canonical_payload(),
            "replication_plan": self.replication_plan.canonical_payload(),
            "warmup": self.warmup,
            "horizon": self.horizon,
            "falsification": self.falsification.canonical_payload(),
            "crn_enabled": self.crn_enabled,
            "report_null_regions": self.report_null_regions,
        }
        if self.cost_analysis is not None:
            payload["cost_analysis"] = self.cost_analysis.canonical_payload()
        if self.surrogate_analysis is not None:
            payload["surrogate_analysis"] = self.surrogate_analysis.canonical_payload()
        return payload

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @property
    def protocol_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()
