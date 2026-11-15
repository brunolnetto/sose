from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
import json
from math import sqrt
from statistics import fmean, stdev
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, InstanceOf, StringConstraints, model_validator

from .agency import AgencyLevel
from .domain_experiment import ExperimentWorld
from .domain_experiment_runtime import (
    DomainExperimentResult,
    ExperimentRunRecord,
    RegimeReference,
)
from .domain_reference import DomainReferenceDescriptor
from .experiment import MetricDirection
from .domain_experiment_pairing import _paired_values, _runs_by_world, _worlds_by_key
from .student_t import (  # noqa: F401 - re-exported for callers and tests
    _beta_continued_fraction,
    _regularized_incomplete_beta,
    _student_t_cdf,
    _student_t_critical,
)


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


def analyze_domain_experiment(
    *,
    reference: _ComparisonDomainReference,
    result: DomainExperimentResult,
) -> DomainExperimentReport:
    """Build generic paired intervention and agency summaries.

    Observed values determine effect magnitude only. Scientific eligibility is
    delegated to the domain and receives configuration/regime context, never
    realized observations.
    """

    protocol = result.plan.protocol
    _require_result_bindings(reference=reference, result=result)
    replications = protocol.replication_plan.min_replications
    if replications != protocol.replication_plan.max_replications:
        raise ValueError("Gate-A analysis requires fixed replication evidence")

    worlds_by_key = _worlds_by_key(result.worlds)
    pair = _EffectPairing(
        protocol=protocol,
        runs_by_world=_runs_by_world(
            worlds=result.worlds,
            runs=result.runs,
            replications=replications,
        ),
        regimes=_regimes_by_world(result),
        reference=reference,
    )
    effects: list[ExperimentEffect] = []
    for design_index in range(protocol.sample_size):
        for agency_level in protocol.agency_levels:
            baseline = worlds_by_key[(design_index, "baseline", agency_level)]
            for arm_id in protocol.intervention_ids:
                treatment = worlds_by_key[(design_index, arm_id, agency_level)]
                effects.extend(pair.effects(ComparisonKind.INTERVENTION, baseline, treatment))
        if AgencyLevel.A0 in protocol.agency_levels:
            effects.extend(_agency_effects(pair, worlds_by_key, design_index))

    return DomainExperimentReport(
        result_hash=result.result_hash,
        protocol_hash=protocol.protocol_hash,
        domain_reference_hash=reference.descriptor.descriptor_hash,
        effects=tuple(effects),
    )


def _require_result_bindings(
    *, reference: _ComparisonDomainReference, result: DomainExperimentResult
) -> None:
    manifest = result.manifest
    if manifest.plan_hash != result.plan.plan_hash:
        raise ValueError("experiment result plan binding is inconsistent")
    if manifest.protocol_hash != result.plan.protocol.protocol_hash:
        raise ValueError("experiment result protocol binding is inconsistent")
    if manifest.world_count != len(result.worlds):
        raise ValueError("experiment result world-count binding is inconsistent")
    if manifest.run_count != len(result.runs):
        raise ValueError("experiment result run-count binding is inconsistent")
    if manifest.result_hash != recompute_domain_experiment_result_hash(result):
        raise ValueError("experiment result hash does not match supplied payload")
    if manifest.domain_reference_hash != reference.descriptor.descriptor_hash:
        raise ValueError("experiment result domain reference binding is inconsistent")


def _regimes_by_world(result: DomainExperimentResult) -> dict[str, RegimeReference]:
    expected_world_hashes = {world.world_hash for world in result.worlds}
    reference_world_hashes = [record.world_hash for record in result.references]
    if (
        len(reference_world_hashes) != len(expected_world_hashes)
        or len(set(reference_world_hashes)) != len(reference_world_hashes)
        or set(reference_world_hashes) != expected_world_hashes
    ):
        raise ValueError("every world requires exactly one regime reference")
    return {record.world_hash: record.regime for record in result.references}


class _EffectPairing:
    """Builds every protocol outcome effect for one control/treatment world pair."""

    def __init__(self, *, protocol, runs_by_world, regimes, reference) -> None:
        self.protocol = protocol
        self.runs_by_world = runs_by_world
        self.regimes = regimes
        self.reference = reference

    def effects(
        self, kind: ComparisonKind, control: ExperimentWorld, treatment: ExperimentWorld
    ) -> list[ExperimentEffect]:
        return [
            _build_effect(
                kind=kind,
                metric_name=outcome.name,
                direction=outcome.direction,
                equivalence_margin=outcome.equivalence_margin,
                confidence_level=self.protocol.statistical_plan.confidence_level,
                control=control,
                treatment=treatment,
                control_runs=self.runs_by_world[control.world_hash],
                treatment_runs=self.runs_by_world[treatment.world_hash],
                control_regime=self.regimes[control.world_hash],
                treatment_regime=self.regimes[treatment.world_hash],
                reference=self.reference,
            )
            for outcome in self.protocol.outcomes
        ]


def _agency_effects(
    pair: _EffectPairing,
    worlds_by_key: dict[tuple[int, str, AgencyLevel], ExperimentWorld],
    design_index: int,
) -> list[ExperimentEffect]:
    effects: list[ExperimentEffect] = []
    for treatment_level in pair.protocol.agency_levels:
        if treatment_level is AgencyLevel.A0:
            continue
        for arm_id in ("baseline", *pair.protocol.intervention_ids):
            control = worlds_by_key[(design_index, arm_id, AgencyLevel.A0)]
            treatment = worlds_by_key[(design_index, arm_id, treatment_level)]
            if control.structural_configuration_hash != treatment.structural_configuration_hash:
                raise ValueError(
                    "agency comparison worlds must share structural configuration"
                )
            effects.extend(pair.effects(ComparisonKind.AGENCY, control, treatment))
    return effects


def _build_effect(
    *,
    kind: ComparisonKind,
    metric_name: str,
    direction: MetricDirection,
    equivalence_margin: float,
    confidence_level: float,
    control: ExperimentWorld,
    treatment: ExperimentWorld,
    control_runs: dict[int, ExperimentRunRecord],
    treatment_runs: dict[int, ExperimentRunRecord],
    control_regime: RegimeReference,
    treatment_regime: RegimeReference,
    reference: _ComparisonDomainReference,
) -> ExperimentEffect:
    if control.crn_group != treatment.crn_group:
        raise ValueError("paired comparison requires a shared CRN group")
    if set(control_runs) != set(treatment_runs):
        raise ValueError("paired run evidence must share exact replication identities")

    control_values, treatment_values, deltas = _paired_values(
        control_runs, treatment_runs, metric_name,
    )

    eligibility = reference.comparison_eligibility(
        ComparisonContext(
            kind=kind,
            metric_name=metric_name,
            control_world=control,
            treatment_world=treatment,
            control_regime=control_regime,
            treatment_regime=treatment_regime,
        )
    )

    mean_delta = fmean(deltas)
    standard_error = stdev(deltas) / sqrt(len(deltas))
    critical = _student_t_critical(
        confidence_level=confidence_level,
        degrees_freedom=len(deltas) - 1,
    )
    ci_half_width = critical * standard_error
    observed_direction = (
        0
        if abs(mean_delta) <= equivalence_margin
        else (-1 if mean_delta < 0.0 else 1)
    )
    favorable: bool | None
    if observed_direction == 0:
        favorable = None
    elif direction is MetricDirection.MINIMIZE:
        favorable = observed_direction < 0
    else:
        favorable = observed_direction > 0

    return ExperimentEffect(
        comparison_kind=kind,
        design_index=control.design_index,
        metric_name=metric_name,
        control_world_hash=control.world_hash,
        treatment_world_hash=treatment.world_hash,
        control_arm_id=control.arm_id,
        treatment_arm_id=treatment.arm_id,
        control_agency_level=control.agency_configuration.level.value,
        treatment_agency_level=treatment.agency_configuration.level.value,
        control_crn_group=control.crn_group,
        treatment_crn_group=treatment.crn_group,
        replications=len(deltas),
        paired=True,
        eligible=eligibility.eligible,
        eligibility_basis=eligibility.basis,
        ineligibility_reason=eligibility.reason,
        control_mean=fmean(control_values),
        treatment_mean=fmean(treatment_values),
        mean_delta=mean_delta,
        paired_standard_error=standard_error,
        confidence_level=confidence_level,
        ci_low=mean_delta - ci_half_width,
        ci_high=mean_delta + ci_half_width,
        equivalence_margin=equivalence_margin,
        direction=observed_direction,
        favorable=favorable,
    )


def recompute_domain_experiment_result_hash(
    result: DomainExperimentResult,
) -> str:
    """Recompute the authoritative digest from the supplied result payload."""

    return _canonical_hash(
        {
            "plan": result.plan.canonical_payload(),
            "worlds": [world.canonical_payload() for world in result.worlds],
            "runs": [run.canonical_payload() for run in result.runs],
            "references": [
                record.canonical_payload() for record in result.references
            ],
            "assessments": [
                assessment.canonical_payload()
                for assessment in result.assessments
            ],
        }
    )


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
