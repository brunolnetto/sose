from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    InstanceOf,
    StringConstraints,
    field_serializer,
    model_validator,
)

from sose.core.randomness import scoped_seed

from .domain_experiment import (
    AgencyConfiguration,
    ExperimentWorld,
    build_experiment_worlds,
)
from .domain_reference import (
    DomainReferenceDescriptor,
    GroundTruthClaim,
    GroundTruthComparisonKind,
    GroundTruthTargetKind,
)
from .experiment import ExperimentProtocol
from .model_spec import ModelIntervention, ModelSpec


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DomainExperimentPlan(BaseModel):
    """Framework-owned deterministic inputs for one fixed-replication experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    protocol: InstanceOf[ExperimentProtocol]
    design_seed: int = Field(ge=0)
    root_seed: int = Field(ge=0)
    agency_configurations: tuple[InstanceOf[AgencyConfiguration], ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def validate_agency_set(self) -> "DomainExperimentPlan":
        levels = [item.level for item in self.agency_configurations]
        if len(set(levels)) != len(levels) or set(levels) != set(
            self.protocol.agency_levels
        ):
            raise ValueError(
                "exactly one agency configuration per protocol level is required"
            )
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "protocol_hash": self.protocol.protocol_hash,
            "design_seed": self.design_seed,
            "root_seed": self.root_seed,
            "agency_configurations": [
                item.canonical_payload()
                for item in sorted(
                    self.agency_configurations,
                    key=lambda item: (item.level.value, item.capability_id),
                )
            ],
        }

    @property
    def plan_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


class ExperimentObservation(BaseModel):
    """Domain-neutral numerical projection from raw domain execution evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metrics: dict[str, float] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "ExperimentObservation":
        for name, value in self.metrics.items():
            if not name.strip():
                raise ValueError("observation metric names must be non-blank")
            if not isfinite(value):
                raise ValueError(f"observation metric {name} must be finite")
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType(dict(sorted(self.metrics.items()))),
        )
        return self

    @field_serializer("metrics")
    def serialize_metrics(self, value: Mapping[str, float]) -> dict[str, float]:
        return dict(value)

    def canonical_payload(self) -> dict[str, object]:
        return {"metrics": dict(self.metrics)}


class RegimeReference(BaseModel):
    """Domain-owned regime classification derived from configured mechanics."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    label: NonBlankString
    stable: bool | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "RegimeReference":
        _validate_json(self.metadata, path="/metadata")
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(
                {
                    key: _freeze_json(value)
                    for key, value in sorted(self.metadata.items())
                }
            ),
        )
        return self

    @field_serializer("metadata")
    def serialize_metadata(self, value: Mapping[str, object]) -> dict[str, object]:
        return {key: _thaw_json(item) for key, item in value.items()}

    def canonical_payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "stable": self.stable,
            "metadata": {
                key: _thaw_json(value) for key, value in self.metadata.items()
            },
        }


class DomainExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    protocol: InstanceOf[ExperimentProtocol]
    world: InstanceOf[ExperimentWorld]
    root_seed: int = Field(ge=0)
    replication: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_protocol_binding(self) -> "DomainExecutionRequest":
        if self.world.protocol_hash != self.protocol.protocol_hash:
            raise ValueError("execution world does not belong to the supplied protocol")
        return self


class DomainExecutionResult(BaseModel):
    """Opaque domain evidence with a deterministic digest owned by the adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    evidence_hash: NonBlankString
    evidence: object

    @model_validator(mode="after")
    def validate_evidence_hash(self) -> "DomainExecutionResult":
        _validate_sha256(self.evidence_hash, field="evidence_hash")
        return self


class _RuntimeDomainReference(Protocol):
    descriptor: DomainReferenceDescriptor

    def build_model(self, point: dict[str, float]) -> ModelSpec: ...

    def interventions(self) -> tuple[ModelIntervention, ...]: ...

    def execute(self, request: DomainExecutionRequest) -> DomainExecutionResult: ...

    def observation(self, result: DomainExecutionResult) -> ExperimentObservation: ...

    def ground_truth(self, world: ExperimentWorld) -> tuple[GroundTruthClaim, ...]: ...

    def classify_regime(self, world: ExperimentWorld) -> RegimeReference: ...


class DomainEvidenceRecord(BaseModel):
    """Opaque raw domain evidence retained for audit/re-projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    world_hash: NonBlankString
    replication: int = Field(ge=0)
    evidence_hash: NonBlankString
    evidence: object

    @model_validator(mode="after")
    def validate_hash(self) -> "DomainEvidenceRecord":
        _validate_sha256(self.evidence_hash, field="evidence_hash")
        return self


class ExperimentRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    world_hash: NonBlankString
    design_index: int = Field(ge=0)
    arm_id: NonBlankString
    agency_level: str
    replication: int = Field(ge=0)
    replication_seed: int = Field(ge=0)
    evidence_hash: NonBlankString
    observation: InstanceOf[ExperimentObservation]

    @model_validator(mode="after")
    def validate_hash(self) -> "ExperimentRunRecord":
        _validate_sha256(self.evidence_hash, field="evidence_hash")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "world_hash": self.world_hash,
            "design_index": self.design_index,
            "arm_id": self.arm_id,
            "agency_level": self.agency_level,
            "replication": self.replication,
            "replication_seed": self.replication_seed,
            "evidence_hash": self.evidence_hash,
            "observation": self.observation.canonical_payload(),
        }


class WorldReferenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    world_hash: NonBlankString
    regime: InstanceOf[RegimeReference]
    ground_truth: tuple[InstanceOf[GroundTruthClaim], ...]

    @model_validator(mode="after")
    def validate_claim_ids(self) -> "WorldReferenceRecord":
        ids = [claim.claim_id for claim in self.ground_truth]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ground-truth claim ids are not allowed per world")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "world_hash": self.world_hash,
            "regime": self.regime.canonical_payload(),
            "ground_truth": [
                claim.canonical_payload()
                for claim in sorted(self.ground_truth, key=lambda item: item.claim_id)
            ],
        }


class GroundTruthAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    world_hash: NonBlankString
    replication: int = Field(ge=0)
    claim_id: NonBlankString
    claim_hash: NonBlankString
    eligible: bool
    observed: float | str | None = Field(default=None)
    passed: bool | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_hash(self) -> "GroundTruthAssessment":
        _validate_sha256(self.claim_hash, field="claim_hash")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class ExperimentResultManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_hash: NonBlankString
    protocol_hash: NonBlankString
    domain_reference_hash: NonBlankString
    world_count: int = Field(ge=1)
    run_count: int = Field(ge=1)
    result_hash: NonBlankString

    @model_validator(mode="after")
    def validate_hashes(self) -> "ExperimentResultManifest":
        for field_name in (
            "plan_hash",
            "protocol_hash",
            "domain_reference_hash",
            "result_hash",
        ):
            _validate_sha256(getattr(self, field_name), field=field_name)
        return self

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class DomainExperimentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    plan: InstanceOf[DomainExperimentPlan]
    worlds: tuple[InstanceOf[ExperimentWorld], ...]
    runs: tuple[InstanceOf[ExperimentRunRecord], ...]
    evidence: tuple[InstanceOf[DomainEvidenceRecord], ...]
    references: tuple[InstanceOf[WorldReferenceRecord], ...]
    assessments: tuple[InstanceOf[GroundTruthAssessment], ...]
    manifest: InstanceOf[ExperimentResultManifest]

    @model_validator(mode="after")
    def validate_evidence_binding(self) -> "DomainExperimentResult":
        run_bindings = {
            (run.world_hash, run.replication): run.evidence_hash
            for run in self.runs
        }
        evidence_bindings = {
            (item.world_hash, item.replication): item.evidence_hash
            for item in self.evidence
        }
        if len(run_bindings) != len(self.runs):
            raise ValueError("duplicate run identities are not allowed")
        if len(evidence_bindings) != len(self.evidence):
            raise ValueError("duplicate evidence identities are not allowed")
        if run_bindings != evidence_bindings:
            raise ValueError("raw evidence must exactly bind every experiment run")
        return self

    @property
    def result_hash(self) -> str:
        return self.manifest.result_hash


def run_domain_experiment(
    *,
    reference: _RuntimeDomainReference,
    plan: DomainExperimentPlan,
) -> DomainExperimentResult:
    """Execute one fixed-replication Gate-A experiment through generic orchestration."""

    replication_plan = plan.protocol.replication_plan
    if replication_plan.min_replications != replication_plan.max_replications:
        raise ValueError("Gate-A runtime requires a fixed replication plan")

    worlds = build_experiment_worlds(
        reference=reference,
        protocol=plan.protocol,
        agency_configurations=plan.agency_configurations,
        design_seed=plan.design_seed,
    )
    references = tuple(
        WorldReferenceRecord(
            world_hash=world.world_hash,
            regime=reference.classify_regime(world),
            ground_truth=reference.ground_truth(world),
        )
        for world in worlds
    )
    claims_by_world = {
        record.world_hash: record.ground_truth
        for record in references
    }
    reference_by_world = {
        record.world_hash: record
        for record in references
    }

    runs: list[ExperimentRunRecord] = []
    evidence_records: list[DomainEvidenceRecord] = []
    assessments: list[GroundTruthAssessment] = []
    for world in worlds:
        for replication in range(replication_plan.min_replications):
            execution = reference.execute(
                DomainExecutionRequest(
                    protocol=plan.protocol,
                    world=world,
                    root_seed=plan.root_seed,
                    replication=replication,
                )
            )
            observation = reference.observation(execution)
            evidence_records.append(
                DomainEvidenceRecord(
                    world_hash=world.world_hash,
                    replication=replication,
                    evidence_hash=execution.evidence_hash,
                    evidence=execution.evidence,
                )
            )
            runs.append(
                ExperimentRunRecord(
                    world_hash=world.world_hash,
                    design_index=world.design_index,
                    arm_id=world.arm_id,
                    agency_level=world.agency_configuration.level.value,
                    replication=replication,
                    replication_seed=scoped_seed(
                        plan.root_seed,
                        world.crn_group,
                        replication,
                    ),
                    evidence_hash=execution.evidence_hash,
                    observation=observation,
                )
            )
            assessments.extend(
                _assess_claim(
                    world_hash=world.world_hash,
                    replication=replication,
                    claim=claim,
                    observation=observation,
                    regime=reference_by_world[world.world_hash].regime,
                )
                for claim in claims_by_world[world.world_hash]
            )

    run_tuple = tuple(runs)
    evidence_tuple = tuple(evidence_records)
    assessment_tuple = tuple(assessments)
    result_hash = _canonical_hash(
        {
            "plan": plan.canonical_payload(),
            "worlds": [world.canonical_payload() for world in worlds],
            "runs": [run.canonical_payload() for run in run_tuple],
            "references": [record.canonical_payload() for record in references],
            "assessments": [
                assessment.canonical_payload()
                for assessment in assessment_tuple
            ],
        }
    )
    manifest = ExperimentResultManifest(
        plan_hash=plan.plan_hash,
        protocol_hash=plan.protocol.protocol_hash,
        domain_reference_hash=reference.descriptor.descriptor_hash,
        world_count=len(worlds),
        run_count=len(run_tuple),
        result_hash=result_hash,
    )
    return DomainExperimentResult(
        plan=plan,
        worlds=worlds,
        runs=run_tuple,
        evidence=evidence_tuple,
        references=references,
        assessments=assessment_tuple,
        manifest=manifest,
    )


def evidence_hash(payload: object) -> str:
    """Hash JSON-serializable raw evidence without standardizing its domain schema."""

    if isinstance(payload, BaseModel):
        value = payload.model_dump(mode="json")
    elif hasattr(payload, "canonical_payload"):
        value = payload.canonical_payload()
    else:
        value = payload
    _validate_json(value, path="/evidence")
    return _canonical_hash(value)


def _assess_claim(
    *,
    world_hash: str,
    replication: int,
    claim: GroundTruthClaim,
    observation: ExperimentObservation,
    regime: RegimeReference,
) -> GroundTruthAssessment:
    if not claim.eligible:
        return GroundTruthAssessment(
            world_hash=world_hash,
            replication=replication,
            claim_id=claim.claim_id,
            claim_hash=claim.claim_hash,
            eligible=False,
            reason=claim.ineligibility_reason,
        )
    if claim.target_kind is GroundTruthTargetKind.REGIME:
        observed_regime = regime.label
        expected_regime = claim.expected
        if (
            claim.comparison_rule.kind is not GroundTruthComparisonKind.EXACT
            or not isinstance(expected_regime, str)
        ):
            return GroundTruthAssessment(
                world_hash=world_hash,
                replication=replication,
                claim_id=claim.claim_id,
                claim_hash=claim.claim_hash,
                eligible=True,
                observed=observed_regime,
                reason="regime claims require an exact string expectation",
            )
        return GroundTruthAssessment(
            world_hash=world_hash,
            replication=replication,
            claim_id=claim.claim_id,
            claim_hash=claim.claim_hash,
            eligible=True,
            observed=observed_regime,
            passed=observed_regime == expected_regime,
        )

    if claim.target_kind is not GroundTruthTargetKind.METRIC:
        return GroundTruthAssessment(
            world_hash=world_hash,
            replication=replication,
            claim_id=claim.claim_id,
            claim_hash=claim.claim_hash,
            eligible=True,
            reason="claim target is not assessed by the Gate-A runtime",
        )

    observed = observation.metrics.get(claim.target_name)
    if observed is None:
        return GroundTruthAssessment(
            world_hash=world_hash,
            replication=replication,
            claim_id=claim.claim_id,
            claim_hash=claim.claim_hash,
            eligible=True,
            reason="standard observation does not expose the claimed metric",
        )
    expected = claim.expected
    if isinstance(expected, bool) or not isinstance(expected, (int, float)):
        return GroundTruthAssessment(
            world_hash=world_hash,
            replication=replication,
            claim_id=claim.claim_id,
            claim_hash=claim.claim_hash,
            eligible=True,
            observed=observed,
            reason="metric claim expected value is not numeric",
        )

    expected_value = float(expected)
    rule = claim.comparison_rule
    if rule.kind is GroundTruthComparisonKind.EXACT:
        passed = observed == expected_value
    elif rule.kind is GroundTruthComparisonKind.ABSOLUTE_TOLERANCE:
        assert rule.tolerance is not None
        passed = abs(observed - expected_value) <= rule.tolerance
    elif rule.kind is GroundTruthComparisonKind.RELATIVE_TOLERANCE:
        assert rule.tolerance is not None
        passed = abs(observed - expected_value) / abs(expected_value) <= rule.tolerance
    elif rule.kind is GroundTruthComparisonKind.LOWER_BOUND:
        passed = observed >= expected_value
    else:
        passed = observed <= expected_value

    return GroundTruthAssessment(
        world_hash=world_hash,
        replication=replication,
        claim_id=claim.claim_id,
        claim_hash=claim.claim_hash,
        eligible=True,
        observed=observed,
        passed=passed,
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


def _validate_sha256(value: str, *, field: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef"
        for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")


def _validate_json(value: object, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"JSON object key at {path} must be a string")
            _validate_json(child, path=f"{path}/{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_json(child, path=f"{path}/{index}")
        return
    raise ValueError(
        f"value at {path} is not JSON-compatible: {type(value).__name__}"
    )


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze_json(child)
                for key, child in sorted(value.items())
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value
