"""Gate-A comparison/effect/report models (split from domain_experiment_analysis)."""
from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
import json
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, InstanceOf, StringConstraints, model_validator

from .domain_experiment import ExperimentWorld
from .domain_experiment_records import RegimeReference
from .domain_reference import DomainReferenceDescriptor


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ComparisonKind(StrEnum):
    INTERVENTION = "intervention"
    AGENCY = "agency"


class ComparisonEligibility(BaseModel):
    """Domain-owned scientific eligibility based only on configured mechanics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible: bool
    basis: tuple[NonBlankString, ...] = Field(min_length=1)
    reason: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_reason(self) -> "ComparisonEligibility":
        if self.eligible and self.reason is not None:
            raise ValueError("eligible comparison cannot have an ineligibility reason")
        if not self.eligible and self.reason is None:
            raise ValueError("ineligible comparison requires a reason")
        if len(set(self.basis)) != len(self.basis):
            raise ValueError("comparison eligibility basis entries must be unique")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "basis": list(self.basis),
            "reason": self.reason,
        }


class ComparisonContext(BaseModel):
    """Configuration-only context given to domain eligibility logic."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    kind: ComparisonKind
    metric_name: NonBlankString
    control_world: InstanceOf[ExperimentWorld]
    treatment_world: InstanceOf[ExperimentWorld]
    control_regime: InstanceOf[RegimeReference]
    treatment_regime: InstanceOf[RegimeReference]


class _ComparisonDomainReference(Protocol):
    descriptor: DomainReferenceDescriptor

    def comparison_eligibility(
        self,
        context: ComparisonContext,
    ) -> ComparisonEligibility: ...


class ExperimentEffect(BaseModel):
    """One generic paired effect summary over a preregistered metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    comparison_kind: ComparisonKind
    design_index: int = Field(ge=0)
    metric_name: NonBlankString
    control_world_hash: NonBlankString
    treatment_world_hash: NonBlankString
    control_arm_id: NonBlankString
    treatment_arm_id: NonBlankString
    control_agency_level: NonBlankString
    treatment_agency_level: NonBlankString
    control_crn_group: NonBlankString
    treatment_crn_group: NonBlankString
    replications: int = Field(ge=2)
    paired: bool
    eligible: bool
    eligibility_basis: tuple[NonBlankString, ...] = Field(min_length=1)
    ineligibility_reason: NonBlankString | None = None
    control_mean: float = Field(allow_inf_nan=False)
    treatment_mean: float = Field(allow_inf_nan=False)
    mean_delta: float = Field(allow_inf_nan=False)
    paired_standard_error: float = Field(ge=0.0, allow_inf_nan=False)
    confidence_level: float = Field(gt=0.0, lt=1.0, allow_inf_nan=False)
    ci_low: float = Field(allow_inf_nan=False)
    ci_high: float = Field(allow_inf_nan=False)
    equivalence_margin: float = Field(gt=0.0, allow_inf_nan=False)
    direction: int = Field(ge=-1, le=1)
    favorable: bool | None = None

    @model_validator(mode="after")
    def validate_effect(self) -> "ExperimentEffect":
        if not self.paired:
            raise ValueError("Gate-A effect summaries require paired CRN evidence")
        if self.eligible and self.ineligibility_reason is not None:
            raise ValueError("eligible effect cannot have ineligibility reason")
        if not self.eligible and self.ineligibility_reason is None:
            raise ValueError("ineligible effect requires ineligibility reason")
        if self.ci_low > self.mean_delta or self.ci_high < self.mean_delta:
            raise ValueError("paired confidence interval must contain mean delta")
        if self.direction == 0 and self.favorable is not None:
            raise ValueError("null-region effect cannot be favorable/unfavorable")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class DomainExperimentReport(BaseModel):
    """Hash-addressed generic report with no composite quality score."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    result_hash: NonBlankString
    protocol_hash: NonBlankString
    domain_reference_hash: NonBlankString
    effects: tuple[InstanceOf[ExperimentEffect], ...]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "result_hash": self.result_hash,
            "protocol_hash": self.protocol_hash,
            "domain_reference_hash": self.domain_reference_hash,
            "effects": [
                effect.canonical_payload()
                for effect in sorted(
                    self.effects,
                    key=lambda item: (
                        item.comparison_kind.value,
                        item.design_index,
                        item.metric_name,
                        item.control_world_hash,
                        item.treatment_world_hash,
                    ),
                )
            ],
        }

    @property
    def report_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


def _canonical_hash(payload: object) -> str:
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
