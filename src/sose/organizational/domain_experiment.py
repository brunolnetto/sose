from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Protocol, runtime_checkable

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

from .agency import AgencyLevel, AgencySpec
from .domain_reference import (
    AgencyCapabilitySpec,
    DomainReferenceDescriptor,
    GroundTruthClaim,
    GroundTruthComparisonKind,
    GroundTruthTargetKind,
)
from .experiment import ExperimentProtocol
from .model_spec import InterventionClass, ModelIntervention, ModelSpec
from .sampling import sample_parameter_space


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgencyConfiguration(BaseModel):
    """One explicit agency capability selection for an experiment protocol."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: AgencyLevel
    capability_id: NonBlankString
    parameters: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "AgencyConfiguration":
        for name, value in self.parameters.items():
            if not name.strip():
                raise ValueError("agency parameter names must be non-blank")
            if not isfinite(value):
                raise ValueError(f"agency parameter {name} must be finite")
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(sorted(self.parameters.items()))),
        )
        return self

    @field_serializer("parameters")
    def serialize_parameters(self, value: Mapping[str, float]) -> dict[str, float]:
        return dict(value)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "capability_id": self.capability_id,
            "parameters": dict(self.parameters),
        }


class DomainExperimentPlan(BaseModel):
    """Framework-owned deterministic experiment construction inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    protocol: InstanceOf[ExperimentProtocol]
    design_seed: int = Field(ge=0)
    root_seed: int = Field(ge=0)
    agency_configurations: tuple[InstanceOf[AgencyConfiguration], ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def validate_agency_configuration_set(self) -> "DomainExperimentPlan":
        levels = [item.level for item in self.agency_configurations]
        if len(set(levels)) != len(levels):
            raise ValueError("exactly one agency configuration per protocol level is required")
        if set(levels) != set(self.protocol.agency_levels):
            raise ValueError("exactly one agency configuration per protocol level is required")
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
                    key=lambda item: item.level.value,
                )
            ],
        }

    @property
    def plan_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


class ExperimentWorld(BaseModel):
    """Immutable configured world. Realized execution outputs are intentionally absent."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    design_index: int = Field(ge=0)
    arm_id: NonBlankString
    agency_level: AgencyLevel
    agency_configuration: InstanceOf[AgencyConfiguration]
    crn_group: NonBlankString
    protocol_hash: NonBlankString
    domain_reference_hash: NonBlankString
    baseline_model_spec_hash: NonBlankString
    exogenous_parameters: dict[str, float]
    intervention_class: InterventionClass | None = None
    intervention_operating_cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    intervention_transition_cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    intervention_transition_time: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    model_spec: InstanceOf[ModelSpec]
    model_spec_hash: NonBlankString
    structural_configuration_hash: NonBlankString

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "ExperimentWorld":
        if self.agency_level is not self.agency_configuration.level:
            raise ValueError("world agency level must match agency configuration")
        if self.model_spec_hash != self.model_spec.model_spec_hash:
            raise ValueError("model_spec_hash must match world ModelSpec")
        if self.model_spec.agency.level is not self.agency_level:
            raise ValueError("world ModelSpec agency must match selected agency level")
        if self.structural_configuration_hash != _structural_hash(self.model_spec):
            raise ValueError("structural_configuration_hash must match non-agency ModelSpec")
        object.__setattr__(
            self,
            "exogenous_parameters",
            MappingProxyType(dict(sorted(self.exogenous_parameters.items()))),
        )
        return self

    @field_serializer("exogenous_parameters")
    def serialize_exogenous_parameters(
        self,
        value: Mapping[str, float],
    ) -> dict[str, float]:
        return dict(value)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "design_index": self.design_index,
            "arm_id": self.arm_id,
            "agency_level": self.agency_level.value,
            "agency_configuration": self.agency_configuration.canonical_payload(),
            "crn_group": self.crn_group,
            "protocol_hash": self.protocol_hash,
            "domain_reference_hash": self.domain_reference_hash,
            "baseline_model_spec_hash": self.baseline_model_spec_hash,
            "exogenous_parameters": dict(self.exogenous_parameters),
            "intervention_class": (
                None if self.intervention_class is None else self.intervention_class.value
            ),
            "intervention_operating_cost": self.intervention_operating_cost,
            "intervention_transition_cost": self.intervention_transition_cost,
            "intervention_transition_time": self.intervention_transition_time,
            "model_spec_hash": self.model_spec_hash,
            "structural_configuration_hash": self.structural_configuration_hash,
        }

    @property
    def world_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


class ExperimentObservation(BaseModel):
    """Domain-neutral numerical observation projected from raw domain evidence."""

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
    """Domain-owned configured-mechanics regime label."""

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
            "metadata": {key: _thaw_json(value) for key, value in self.metadata.items()},
        }


class DomainExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    protocol: InstanceOf[ExperimentProtocol]
    world: InstanceOf[ExperimentWorld]
    root_seed: int = Field(ge=0)
    replication: int = Field(ge=0)


class DomainExecutionResult(BaseModel):
    """Raw domain evidence plus its canonical evidence digest."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    evidence_hash: NonBlankString
    evidence: object

    @model_validator(mode="after")
    def validate_hash(self) -> "DomainExecutionResult":
        _validate_sha256(self.evidence_hash, field="evidence_hash")
        return self


@runtime_checkable
class DomainReference(Protocol):
    """Provisional organizational-layer extension contract from TRD-0001."""

    @property
    def descriptor(self) -> DomainReferenceDescriptor: ...

    def baseline_model(self) -> ModelSpec: ...

    def build_model(self, point: Mapping[str, float]) -> ModelSpec: ...

    def interventions(self) -> tuple[ModelIntervention, ...]: ...

    def agency_spec(self, configuration: AgencyConfiguration) -> AgencySpec: ...

    def execute(self, request: DomainExecutionRequest) -> DomainExecutionResult: ...

    def observation(self, result: DomainExecutionResult) -> ExperimentObservation: ...

    def ground_truth(self, world: ExperimentWorld) -> tuple[GroundTruthClaim, ...]: ...

    def classify_regime(self, world: ExperimentWorld) -> RegimeReference: ...


class ExperimentRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    world_hash: NonBlankString
    design_index: int = Field(ge=0)
    arm_id: NonBlankString
    agency_level: AgencyLevel
    replication: int = Field(ge=0)
    replication_seed: int = Field(ge=0)
    evidence_hash: NonBlankString
    observation: InstanceOf[ExperimentObservation]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "world_hash": self.world_hash,
            "design_index": self.design_index,
            "arm_id": self.arm_id,
            "agency_level": self.agency_level.value,
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

    def canonical_payload(self) -> dict[str, object]:
        return {
            "world_hash": self.world_hash,
            "regime": self.regime.canonical_payload(),
            "ground_truth": [
                item.canonical_payload()
                for item in sorted(self.ground_truth, key=lambda claim: claim.claim_id)
            ],
        }


class GroundTruthAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    world_hash: NonBlankString
    replication: int = Field(ge=0)
    claim_id: NonBlankString
    claim_hash: NonBlankString
    eligible: bool
    observed: float | None = Field(default=None, allow_inf_nan=False)
    passed: bool | None = None
    reason: str | None = None

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
        for field in (
            "plan_hash",
            "protocol_hash",
            "domain_reference_hash",
            "result_hash",
        ):
            _validate_sha256(getattr(self, field), field=field)
        return self

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class DomainExperimentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    plan: InstanceOf[DomainExperimentPlan]
    worlds: tuple[InstanceOf[ExperimentWorld], ...]
    runs: tuple[InstanceOf[ExperimentRunRecord], ...]
    references: tuple[InstanceOf[WorldReferenceRecord], ...]
    assessments: tuple[InstanceOf[GroundTruthAssessment], ...]
    manifest: InstanceOf[ExperimentResultManifest]

    @property
    def result_hash(self) -> str:
        return self.manifest.result_hash


def build_domain_experiment_worlds(
    *,
    reference: DomainReference,
    plan: DomainExperimentPlan,
) -> tuple[ExperimentWorld, ...]:
    """Framework-owned deterministic DOE/world expansion."""

    _validate_reference_plan(reference=reference, plan=plan)
    descriptor = reference.descriptor
    protocol = plan.protocol
    intervention_by_id = {
        item.intervention_id: item for item in reference.interventions()
    }
    configurations = {
        item.level: _resolve_agency_configuration(
            descriptor=descriptor,
            configuration=item,
        )
        for item in plan.agency_configurations
    }

    points = sample_parameter_space(
        parameter_ranges=protocol.parameter_ranges,
        design=protocol.sampling_design,
        sample_size=protocol.sample_size,
        seed=plan.design_seed,
    )
    arms: tuple[tuple[str, ModelIntervention | None], ...] = (
        ("baseline", None),
        *tuple(
            (identifier, intervention_by_id[identifier])
            for identifier in protocol.intervention_ids
        ),
    )
    worlds: list[ExperimentWorld] = []

    for design_index, point in enumerate(points):
        point_model = reference.build_model(point)
        for name, value in point.items():
            if name not in point_model.parameters or float(point_model.parameters[name]) != value:
                raise ValueError(
                    f"domain build_model must bind sampled exogenous parameter {name}"
                )

        for level in protocol.agency_levels:
            configuration = configurations[level]
            agency = reference.agency_spec(configuration)
            if agency.level is not level:
                raise ValueError("domain agency_spec returned the wrong agency level")
            agency_model = _with_agency(point_model, agency)

            for arm_id, intervention in arms:
                if intervention is None:
                    model = agency_model
                    intervention_class = None
                    operating_cost = transition_cost = transition_time = 0.0
                else:
                    model, _ = intervention.apply(agency_model)
                    if model.agency.level is not level:
                        raise ValueError("intervention cannot rewrite selected agency level")
                    intervention_class = intervention.intervention_class
                    operating_cost = intervention.operating_cost
                    transition_cost = intervention.transition_cost
                    transition_time = intervention.transition_time

                worlds.append(
                    ExperimentWorld(
                        design_index=design_index,
                        arm_id=arm_id,
                        agency_level=level,
                        agency_configuration=configuration,
                        crn_group=f"design:{design_index}",
                        protocol_hash=protocol.protocol_hash,
                        domain_reference_hash=descriptor.descriptor_hash,
                        baseline_model_spec_hash=reference.baseline_model().model_spec_hash,
                        exogenous_parameters=dict(point),
                        intervention_class=intervention_class,
                        intervention_operating_cost=operating_cost,
                        intervention_transition_cost=transition_cost,
                        intervention_transition_time=transition_time,
                        model_spec=model,
                        model_spec_hash=model.model_spec_hash,
                        structural_configuration_hash=_structural_hash(model),
                    )
                )

    return tuple(worlds)


def run_domain_experiment(
    *,
    reference: DomainReference,
    plan: DomainExperimentPlan,
) -> DomainExperimentResult:
    """Execute a fixed-replication Gate-A experiment through generic orchestration."""

    replication_plan = plan.protocol.replication_plan
    if replication_plan.min_replications != replication_plan.max_replications:
        raise ValueError("Gate-A generic orchestration requires a fixed replication count")

    worlds = build_domain_experiment_worlds(reference=reference, plan=plan)
    references = tuple(
        WorldReferenceRecord(
            world_hash=world.world_hash,
            regime=reference.classify_regime(world),
            ground_truth=reference.ground_truth(world),
        )
        for world in worlds
    )
    claims_by_world = {
        item.world_hash: item.ground_truth
        for item in references
    }

    runs: list[ExperimentRunRecord] = []
    assessments: list[GroundTruthAssessment] = []
    for world in worlds:
        for replication in range(replication_plan.min_replications):
            request = DomainExecutionRequest(
                protocol=plan.protocol,
                world=world,
                root_seed=plan.root_seed,
                replication=replication,
            )
            raw = reference.execute(request)
            observation = reference.observation(raw)
            record = ExperimentRunRecord(
                world_hash=world.world_hash,
                design_index=world.design_index,
                arm_id=world.arm_id,
                agency_level=world.agency_level,
                replication=replication,
                replication_seed=scoped_seed(
                    plan.root_seed,
                    world.crn_group,
                    replication,
                ),
                evidence_hash=raw.evidence_hash,
                observation=observation,
            )
            runs.append(record)
            assessments.extend(
                _assess_claim(
                    world_hash=world.world_hash,
                    replication=replication,
                    claim=claim,
                    observation=observation,
                )
                for claim in claims_by_world[world.world_hash]
            )

    world_tuple = tuple(worlds)
    run_tuple = tuple(runs)
    reference_tuple = tuple(references)
    assessment_tuple = tuple(assessments)
    result_hash = _canonical_hash(
        {
            "plan": plan.canonical_payload(),
            "worlds": [world.canonical_payload() for world in world_tuple],
            "runs": [run.canonical_payload() for run in run_tuple],
            "references": [item.canonical_payload() for item in reference_tuple],
            "assessments": [item.canonical_payload() for item in assessment_tuple],
        }
    )
    manifest = ExperimentResultManifest(
        plan_hash=plan.plan_hash,
        protocol_hash=plan.protocol.protocol_hash,
        domain_reference_hash=reference.descriptor.descriptor_hash,
        world_count=len(world_tuple),
        run_count=len(run_tuple),
        result_hash=result_hash,
    )
    return DomainExperimentResult(
        plan=plan,
        worlds=world_tuple,
        runs=run_tuple,
        references=reference_tuple,
        assessments=assessment_tuple,
        manifest=manifest,
    )


def evidence_hash(payload: object) -> str:
    """Hash JSON-serializable domain evidence without owning its schema."""

    if isinstance(payload, BaseModel):
        value = payload.model_dump(mode="json")
    elif hasattr(payload, "canonical_payload"):
        value = payload.canonical_payload()
    else:
        value = payload
    _validate_json(value, path="/evidence")
    return _canonical_hash(value)


def _validate_reference_plan(
    *,
    reference: DomainReference,
    plan: DomainExperimentPlan,
) -> None:
    descriptor = reference.descriptor
    baseline = reference.baseline_model()
    protocol = plan.protocol

    if baseline.model_spec_hash != protocol.baseline_model_spec_hash:
        raise ValueError("baseline ModelSpec hash does not match experiment protocol")

    parameter_by_name = {item.name: item for item in descriptor.parameters}
    for name, requested in protocol.parameter_ranges.items():
        definition = parameter_by_name.get(name)
        if definition is None:
            raise ValueError(f"protocol parameter {name} is not exposed by domain reference")
        if requested.low < definition.range.low or requested.high > definition.range.high:
            raise ValueError(
                f"protocol parameter {name} range is outside domain-declared range"
            )

    interventions = reference.interventions()
    ids = [item.intervention_id for item in interventions]
    if len(set(ids)) != len(ids):
        raise ValueError("domain reference exposes duplicate intervention ids")
    if set(ids) != set(protocol.intervention_ids):
        raise ValueError("domain intervention ids must exactly match protocol")

    for configuration in plan.agency_configurations:
        _resolve_agency_configuration(
            descriptor=descriptor,
            configuration=configuration,
        )


def _resolve_agency_configuration(
    *,
    descriptor: DomainReferenceDescriptor,
    configuration: AgencyConfiguration,
) -> AgencyConfiguration:
    matches = [
        capability
        for capability in descriptor.agency_capabilities
        if capability.level is configuration.level
        and capability.capability_id == configuration.capability_id
    ]
    if len(matches) != 1:
        raise ValueError(
            "unsupported agency capability "
            f"{configuration.level.value}/{configuration.capability_id}"
        )
    capability: AgencyCapabilitySpec = matches[0]
    values = dict(capability.defaults)
    unknown = set(configuration.parameters) - set(capability.parameter_ranges)
    if unknown:
        raise ValueError(
            "unknown agency capability parameter(s): " + ", ".join(sorted(unknown))
        )
    values.update(configuration.parameters)
    for name, value in values.items():
        bounds = capability.parameter_ranges[name]
        if not bounds.low <= value <= bounds.high:
            raise ValueError(
                f"agency parameter {name} is outside capability range"
            )
    return AgencyConfiguration(
        level=configuration.level,
        capability_id=configuration.capability_id,
        parameters=values,
    )


def _assess_claim(
    *,
    world_hash: str,
    replication: int,
    claim: GroundTruthClaim,
    observation: ExperimentObservation,
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
    if claim.target_kind is not GroundTruthTargetKind.METRIC:
        return GroundTruthAssessment(
            world_hash=world_hash,
            replication=replication,
            claim_id=claim.claim_id,
            claim_hash=claim.claim_hash,
            eligible=True,
            reason="eligible claim is not a standard metric assessment",
        )
    observed = observation.metrics.get(claim.target_name)
    if observed is None:
        return GroundTruthAssessment(
            world_hash=world_hash,
            replication=replication,
            claim_id=claim.claim_id,
            claim_hash=claim.claim_hash,
            eligible=True,
            reason="standard observation does not expose claimed metric",
        )
    if isinstance(claim.expected, bool) or not isinstance(claim.expected, (int, float)):
        return GroundTruthAssessment(
            world_hash=world_hash,
            replication=replication,
            claim_id=claim.claim_id,
            claim_hash=claim.claim_hash,
            eligible=True,
            observed=observed,
            reason="metric claim expected value is not numeric",
        )
    expected = float(claim.expected)
    rule = claim.comparison_rule
    if rule.kind is GroundTruthComparisonKind.EXACT:
        passed = observed == expected
    elif rule.kind is GroundTruthComparisonKind.ABSOLUTE_TOLERANCE:
        assert rule.tolerance is not None
        passed = abs(observed - expected) <= rule.tolerance
    elif rule.kind is GroundTruthComparisonKind.RELATIVE_TOLERANCE:
        assert rule.tolerance is not None
        passed = abs(observed - expected) / abs(expected) <= rule.tolerance
    elif rule.kind is GroundTruthComparisonKind.LOWER_BOUND:
        passed = observed >= expected
    else:
        passed = observed <= expected
    return GroundTruthAssessment(
        world_hash=world_hash,
        replication=replication,
        claim_id=claim.claim_id,
        claim_hash=claim.claim_hash,
        eligible=True,
        observed=observed,
        passed=passed,
    )


def _with_agency(spec: ModelSpec, agency: AgencySpec) -> ModelSpec:
    payload = spec.canonical_payload()
    payload["agency"] = agency.canonical_payload()
    return ModelSpec.model_validate(payload)


def _structural_hash(spec: ModelSpec) -> str:
    payload = spec.canonical_payload()
    payload.pop("agency", None)
    return _canonical_hash(payload)


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
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
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
    raise ValueError(f"value at {path} is not JSON-compatible: {type(value).__name__}")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(child) for key, child in sorted(value.items())}
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
