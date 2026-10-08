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

from .agency import AgencyLevel
from .domain_reference import (
    AgencyCapabilitySpec,
    DomainReferenceDescriptor,
    DomainReferenceIdentity,
)
from .experiment import ExperimentProtocol
from .model_spec import InterventionClass, ModelIntervention, ModelSpec
from .sampling import sample_parameter_space


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _WorldConstructionReference(Protocol):
    descriptor: DomainReferenceDescriptor

    def build_model(self, point: dict[str, float]) -> ModelSpec: ...

    def interventions(self) -> tuple[ModelIntervention, ...]: ...


class AgencyConfiguration(BaseModel):
    """One resolved domain-owned agency configuration selected for an experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    level: AgencyLevel
    capability_id: NonBlankString
    parameters: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "AgencyConfiguration":
        if self.level is AgencyLevel.A3:
            raise ValueError("A3 is outside the SOSE 1.0 experiment contract")
        for name, value in self.parameters.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("agency parameter names must be non-empty")
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

    @property
    def configuration_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


class ExperimentWorld(BaseModel):
    """Framework-owned immutable configuration for one domain/arm/agency world."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    domain_reference: InstanceOf[DomainReferenceIdentity]
    domain_reference_hash: NonBlankString
    design_index: int = Field(ge=0)
    arm_id: NonBlankString
    agency_configuration: InstanceOf[AgencyConfiguration]
    crn_group: NonBlankString
    protocol_hash: NonBlankString
    baseline_model_spec_hash: NonBlankString
    exogenous_parameters: dict[str, float]
    intervention_id: NonBlankString | None = None
    intervention_class: InterventionClass | None = None
    intervention_hash: NonBlankString | None = None
    intervention_operating_cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    intervention_transition_cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    intervention_transition_time: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    model_spec: InstanceOf[ModelSpec]
    model_spec_hash: NonBlankString

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "ExperimentWorld":
        if self.domain_reference_hash != self.domain_reference.reference_hash:
            raise ValueError("domain_reference_hash must match DomainReferenceIdentity")
        if self.model_spec_hash != self.model_spec.model_spec_hash:
            raise ValueError("model_spec_hash must match world ModelSpec")

        if self.arm_id == "baseline":
            if any(
                value is not None
                for value in (
                    self.intervention_id,
                    self.intervention_class,
                    self.intervention_hash,
                )
            ):
                raise ValueError("baseline world cannot declare intervention identity")
            if any(
                value != 0.0
                for value in (
                    self.intervention_operating_cost,
                    self.intervention_transition_cost,
                    self.intervention_transition_time,
                )
            ):
                raise ValueError("baseline world cannot declare intervention costs")
        else:
            if self.intervention_id != self.arm_id:
                raise ValueError("non-baseline arm must match intervention_id")
            if self.intervention_class is None or self.intervention_hash is None:
                raise ValueError("non-baseline world requires intervention metadata")

        for name, value in self.exogenous_parameters.items():
            if not isfinite(value):
                raise ValueError(f"exogenous parameter {name} must be finite")
        object.__setattr__(
            self,
            "exogenous_parameters",
            MappingProxyType(dict(sorted(self.exogenous_parameters.items()))),
        )
        return self

    @field_serializer("domain_reference")
    def serialize_domain_reference(
        self,
        value: DomainReferenceIdentity,
    ) -> dict[str, object]:
        return value.canonical_payload()

    @field_serializer("agency_configuration")
    def serialize_agency_configuration(
        self,
        value: AgencyConfiguration,
    ) -> dict[str, object]:
        return value.canonical_payload()

    @field_serializer("exogenous_parameters")
    def serialize_exogenous_parameters(
        self,
        value: Mapping[str, float],
    ) -> dict[str, float]:
        return dict(value)

    @field_serializer("model_spec")
    def serialize_model_spec(self, value: ModelSpec) -> dict[str, object]:
        return value.canonical_payload()

    @property
    def agency_configuration_hash(self) -> str:
        return self.agency_configuration.configuration_hash

    @property
    def structural_configuration_hash(self) -> str:
        return self.model_spec_hash

    def canonical_payload(self) -> dict[str, object]:
        return {
            "domain_reference": self.domain_reference.canonical_payload(),
            "domain_reference_hash": self.domain_reference_hash,
            "design_index": self.design_index,
            "arm_id": self.arm_id,
            "agency_configuration": self.agency_configuration.canonical_payload(),
            "agency_configuration_hash": self.agency_configuration_hash,
            "crn_group": self.crn_group,
            "protocol_hash": self.protocol_hash,
            "baseline_model_spec_hash": self.baseline_model_spec_hash,
            "exogenous_parameters": dict(self.exogenous_parameters),
            "intervention_id": self.intervention_id,
            "intervention_class": (
                None
                if self.intervention_class is None
                else self.intervention_class.value
            ),
            "intervention_hash": self.intervention_hash,
            "intervention_operating_cost": self.intervention_operating_cost,
            "intervention_transition_cost": self.intervention_transition_cost,
            "intervention_transition_time": self.intervention_transition_time,
            "model_spec_hash": self.model_spec_hash,
            "structural_configuration_hash": self.structural_configuration_hash,
        }

    @property
    def world_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


def build_experiment_worlds(
    *,
    reference: _WorldConstructionReference,
    protocol: ExperimentProtocol,
    agency_configurations: tuple[AgencyConfiguration, ...],
    design_seed: int,
) -> tuple[ExperimentWorld, ...]:
    """Expand a protocol into canonical worlds without delegating orchestration.

    The reference supplies domain semantics. DOE sampling, design indices, arm
    expansion, agency expansion, world identity, and CRN grouping are framework-owned.
    """

    descriptor = reference.descriptor
    parameter_by_name = {item.name: item for item in descriptor.parameters}
    _validate_protocol_parameter_space(
        protocol=protocol,
        parameter_by_name=parameter_by_name,
    )
    agency_by_level = _validate_agency_configurations(
        protocol=protocol,
        descriptor=descriptor,
        configurations=agency_configurations,
    )

    defaults = {
        name: definition.default
        for name, definition in parameter_by_name.items()
    }
    baseline = reference.build_model(dict(defaults))
    _validate_built_model(
        model=baseline,
        configured_point=defaults,
    )
    if baseline.model_spec_hash != protocol.baseline_model_spec_hash:
        raise ValueError(
            "protocol baseline_model_spec_hash does not match the domain reference default model"
        )

    interventions = reference.interventions()
    intervention_by_id = {item.intervention_id: item for item in interventions}
    if "baseline" in intervention_by_id:
        raise ValueError("baseline is a reserved experiment arm identifier")
    if len(intervention_by_id) != len(interventions):
        raise ValueError("duplicate intervention ids are not allowed")
    unknown_interventions = sorted(
        set(protocol.intervention_ids) - set(intervention_by_id)
    )
    if unknown_interventions:
        raise ValueError(
            "experiment protocol requests unknown intervention id(s): "
            + ", ".join(unknown_interventions)
        )

    sampled_points = sample_parameter_space(
        parameter_ranges=protocol.parameter_ranges,
        design=protocol.sampling_design,
        sample_size=protocol.sample_size,
        seed=design_seed,
    )
    arms: tuple[tuple[str, ModelIntervention | None], ...] = (
        ("baseline", None),
        *tuple(
            (identifier, intervention_by_id[identifier])
            for identifier in protocol.intervention_ids
        ),
    )
    ordered_agency = tuple(
        sorted(
            agency_by_level.values(),
            key=lambda item: (item.level.value, item.capability_id),
        )
    )

    worlds: list[ExperimentWorld] = []
    for design_index, sampled in enumerate(sampled_points):
        configured_point = dict(defaults)
        configured_point.update(sampled)
        point_model = reference.build_model(dict(configured_point))
        _validate_built_model(
            model=point_model,
            configured_point=configured_point,
        )
        crn_group = (
            f"{descriptor.identity.reference_id}:"
            f"{descriptor.identity.reference_version}:design:{design_index}"
        )

        for arm_id, intervention in arms:
            if intervention is None:
                arm_model = point_model
                intervention_id = None
                intervention_class = None
                intervention_hash = None
                operating_cost = 0.0
                transition_cost = 0.0
                transition_time = 0.0
            else:
                arm_model, _ = intervention.apply(point_model)
                intervention_id = intervention.intervention_id
                intervention_class = intervention.intervention_class
                intervention_hash = intervention.intervention_hash
                operating_cost = intervention.operating_cost
                transition_cost = intervention.transition_cost
                transition_time = intervention.transition_time

            for agency_configuration in ordered_agency:
                worlds.append(
                    ExperimentWorld(
                        domain_reference=descriptor.identity,
                        domain_reference_hash=descriptor.identity.reference_hash,
                        design_index=design_index,
                        arm_id=arm_id,
                        agency_configuration=agency_configuration,
                        crn_group=crn_group,
                        protocol_hash=protocol.protocol_hash,
                        baseline_model_spec_hash=protocol.baseline_model_spec_hash,
                        exogenous_parameters=configured_point,
                        intervention_id=intervention_id,
                        intervention_class=intervention_class,
                        intervention_hash=intervention_hash,
                        intervention_operating_cost=operating_cost,
                        intervention_transition_cost=transition_cost,
                        intervention_transition_time=transition_time,
                        model_spec=arm_model,
                        model_spec_hash=arm_model.model_spec_hash,
                    )
                )

    return tuple(worlds)


def _validate_protocol_parameter_space(
    *,
    protocol: ExperimentProtocol,
    parameter_by_name: Mapping[str, object],
) -> None:
    undeclared = sorted(set(protocol.parameter_ranges) - set(parameter_by_name))
    if undeclared:
        raise ValueError(
            "DOE axes must be declared domain parameters; undeclared: "
            + ", ".join(undeclared)
        )

    for name, requested in protocol.parameter_ranges.items():
        definition = parameter_by_name[name]
        allowed = definition.range
        if requested.low < allowed.low or requested.high > allowed.high:
            raise ValueError(
                f"DOE range for {name} is outside the domain-declared range"
            )


def _validate_agency_configurations(
    *,
    protocol: ExperimentProtocol,
    descriptor: DomainReferenceDescriptor,
    configurations: tuple[AgencyConfiguration, ...],
) -> dict[AgencyLevel, AgencyConfiguration]:
    by_level: dict[AgencyLevel, AgencyConfiguration] = {}
    for configuration in configurations:
        if configuration.level in by_level:
            raise ValueError("only one agency configuration per level is allowed")
        by_level[configuration.level] = configuration

    if set(by_level) != set(protocol.agency_levels):
        raise ValueError(
            "agency configurations must exactly match protocol agency levels"
        )

    capability_by_key: dict[tuple[AgencyLevel, str], AgencyCapabilitySpec] = {
        (capability.level, capability.capability_id): capability
        for capability in descriptor.agency_capabilities
    }
    for configuration in configurations:
        key = (configuration.level, configuration.capability_id)
        capability = capability_by_key.get(key)
        if capability is None:
            raise ValueError(
                "selected agency capability is not declared by the domain reference"
            )
        if set(configuration.parameters) != set(capability.parameter_ranges):
            raise ValueError(
                "agency configuration parameters must exactly match the capability schema"
            )
        for name, value in configuration.parameters.items():
            bounds = capability.parameter_ranges[name]
            if value < bounds.low or value > bounds.high:
                raise ValueError(
                    f"agency parameter {name} is outside its declared range"
                )

    return by_level


def _validate_built_model(
    *,
    model: ModelSpec,
    configured_point: Mapping[str, float],
) -> None:
    for name, expected in configured_point.items():
        actual = model.parameters.get(name)
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not isfinite(float(actual))
            or float(actual) != expected
        ):
            raise ValueError(
                "domain build_model must preserve the configured exogenous point "
                f"for parameter {name}"
            )


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
