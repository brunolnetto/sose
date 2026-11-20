from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, InstanceOf, field_serializer, field_validator, model_validator

from .agency import AgencyLevel
from .domain_reference_ground_truth import (
    GroundTruthClaim,
    GroundTruthComparisonKind,
    GroundTruthComparisonRule,
    GroundTruthKind,
    GroundTruthTargetKind,
    NonBlankString,
    _canonical_hash,
    _require_unique,
)
from .experiment import ParameterRange
from .model_spec import EvidenceClass

# Re-exported classes here keep this module as their __module__ (codec and
# deterministic_id class paths), so they must stay importable from it.
__all__ = [
    "AgencyCapabilitySpec", "DomainReference", "DomainReferenceDescriptor",
    "DomainReferenceIdentity", "ExperimentObservation", "GroundTruthClaim",
    "GroundTruthComparisonKind", "GroundTruthComparisonRule", "GroundTruthKind",
    "GroundTruthTargetKind", "ParameterDefinition", "ParameterSpaceSpec",
]


class DomainReferenceIdentity(BaseModel):
    """Stable identity for one experiment-capable domain reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: NonBlankString
    reference_id: NonBlankString
    reference_version: NonBlankString
    process_manifest_domain: NonBlankString
    specification_path: NonBlankString

    @model_validator(mode="after")
    def validate_specification_path(self) -> "DomainReferenceIdentity":
        if not _is_repository_relative(self.specification_path):
            raise ValueError("specification_path must be repository-relative")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "reference_id": self.reference_id,
            "reference_version": self.reference_version,
            "process_manifest_domain": self.process_manifest_domain,
            "specification_path": self.specification_path,
        }

    @property
    def reference_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


class ParameterDefinition(BaseModel):
    """One user-configurable exogenous coordinate owned by a domain reference."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: NonBlankString
    description: NonBlankString
    units: NonBlankString | None = None
    default: float = Field(allow_inf_nan=False)
    range: InstanceOf[ParameterRange]
    evidence_class: EvidenceClass

    @model_validator(mode="after")
    def validate_default(self) -> "ParameterDefinition":
        if not self.range.low <= self.default <= self.range.high:
            raise ValueError("parameter default must lie within its declared exogenous range")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "units": self.units,
            "default": self.default,
            "range": self.range.canonical_payload(),
            "evidence_class": self.evidence_class.value,
        }


class AgencyCapabilitySpec(BaseModel):
    """Domain-owned configuration schema for one supported agency capability."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    level: AgencyLevel
    capability_id: NonBlankString
    parameter_ranges: dict[str, ParameterRange] = Field(default_factory=dict)
    defaults: dict[str, float] = Field(default_factory=dict)
    compatibility_constraints: tuple[NonBlankString, ...] = ()
    observation_contract_ids: tuple[NonBlankString, ...] = ()
    action_contract_ids: tuple[NonBlankString, ...] = ()

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "AgencyCapabilitySpec":
        if self.level is AgencyLevel.A3:
            raise ValueError("A3 is outside the SOSE 1.0 experiment capability contract")

        range_names = set(self.parameter_ranges)
        default_names = set(self.defaults)
        if range_names != default_names:
            raise ValueError("agency capability defaults must exactly match parameter ranges")

        _require_defaults_in_ranges(self.defaults, self.parameter_ranges)
        has_contracts = (bool(self.observation_contract_ids), bool(self.action_contract_ids))
        if self.level is AgencyLevel.A2:
            if not all(has_contracts):
                raise ValueError("A2 requires observation and action contracts")
        elif any(has_contracts):
            raise ValueError("observation/action contracts are only valid for A2")

        _require_unique(self.compatibility_constraints, "agency compatibility constraints")
        _require_unique(self.observation_contract_ids, "observation contract ids")
        _require_unique(self.action_contract_ids, "action contract ids")

        object.__setattr__(
            self,
            "parameter_ranges",
            MappingProxyType(dict(sorted(self.parameter_ranges.items()))),
        )
        object.__setattr__(
            self,
            "defaults",
            MappingProxyType(dict(sorted(self.defaults.items()))),
        )
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "capability_id": self.capability_id,
            "parameter_ranges": {
                name: bounds.canonical_payload()
                for name, bounds in self.parameter_ranges.items()
            },
            "defaults": dict(self.defaults),
            "compatibility_constraints": sorted(self.compatibility_constraints),
            "observation_contract_ids": sorted(self.observation_contract_ids),
            "action_contract_ids": sorted(self.action_contract_ids),
        }

    @field_serializer("parameter_ranges")
    def serialize_parameter_ranges(
        self,
        value: Mapping[str, ParameterRange],
    ) -> dict[str, object]:
        return {
            name: bounds.canonical_payload()
            for name, bounds in value.items()
        }

    @field_serializer("defaults")
    def serialize_defaults(self, value: Mapping[str, float]) -> dict[str, float]:
        return dict(value)

    @property
    def capability_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


class DomainReferenceDescriptor(BaseModel):
    """Hash-addressed semantic capabilities exposed by one DomainReference."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    identity: InstanceOf[DomainReferenceIdentity]
    parameters: tuple[InstanceOf[ParameterDefinition], ...] = Field(min_length=1)
    agency_capabilities: tuple[InstanceOf[AgencyCapabilitySpec], ...] = Field(
        min_length=1
    )
    ground_truth_kinds: tuple[GroundTruthKind, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_uniqueness(self) -> "DomainReferenceDescriptor":
        _require_unique([item.name for item in self.parameters], "parameter names")
        _require_unique(
            [(item.level.value, item.capability_id) for item in self.agency_capabilities],
            "agency capability declarations",
        )
        _require_unique(self.ground_truth_kinds, "ground-truth kinds")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "identity": self.identity.canonical_payload(),
            "parameters": [
                item.canonical_payload()
                for item in sorted(self.parameters, key=lambda value: value.name)
            ],
            "agency_capabilities": [
                item.canonical_payload()
                for item in sorted(
                    self.agency_capabilities,
                    key=lambda value: (value.level.value, value.capability_id),
                )
            ],
            "ground_truth_kinds": sorted(
                item.value for item in self.ground_truth_kinds
            ),
        }

    @property
    def descriptor_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


class ParameterSpaceSpec(BaseModel):
    """Hash-stable exogenous parameter space declared by one domain reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parameters: tuple[InstanceOf[ParameterDefinition], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_uniqueness(self) -> "ParameterSpaceSpec":
        _require_unique([item.name for item in self.parameters], "parameter names")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "parameters": [
                item.canonical_payload()
                for item in sorted(self.parameters, key=lambda value: value.name)
            ]
        }

    @property
    def parameter_space_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


class ExperimentObservation(BaseModel):
    """Domain-neutral projection consumed by generic experiment analysis/reporting."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    world_hash: NonBlankString
    replication: int = Field(ge=0)
    metrics: dict[str, float] = Field(min_length=1)

    @field_validator("metrics", mode="before")
    @classmethod
    def reject_ambiguous_metrics(cls, metrics: object) -> object:
        # Pydantic coerces bool to 1.0 and string numerals to floats by default.
        # Neither is an observed numeric metric with verifiable source semantics.
        if not isinstance(metrics, Mapping):
            raise ValueError("observation metrics must be a mapping")
        for name, value in metrics.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("observation metric names must be non-blank strings")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"observation metric {name} must be a numeric value, not bool or text")
            try:
                finite = isfinite(value)
            except (OverflowError, TypeError, ValueError):
                finite = False
            if not finite:
                raise ValueError(f"observation metric {name} must be finite")
        return metrics

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


@runtime_checkable
class DomainReference(Protocol):
    """Provisional domain extension boundary from ADR-0002/TRD-0001."""

    @property
    def identity(self) -> DomainReferenceIdentity: ...

    def descriptor(self) -> DomainReferenceDescriptor: ...

    def parameter_space(self) -> ParameterSpaceSpec: ...

    def agency_capabilities(self) -> tuple[AgencyCapabilitySpec, ...]: ...

    def ground_truth(self, world: object) -> tuple[GroundTruthClaim, ...]: ...

    def classify_regime(self, world: object) -> object: ...

    def execute(
        self,
        *,
        protocol: object,
        world: object,
        root_seed: int,
        replication: int,
    ) -> object: ...

    def observation(self, result: object) -> ExperimentObservation: ...


def _require_defaults_in_ranges(defaults, parameter_ranges) -> None:
    for name, value in defaults.items():
        if not isfinite(value):
            raise ValueError(f"agency capability default for {name} must be finite")
        bounds = parameter_ranges[name]
        if not bounds.low <= value <= bounds.high:
            raise ValueError(
                f"agency capability default for {name} must lie within its range"
            )


def _is_repository_relative(path: str) -> bool:
    if not path or "\\" in path:
        return False
    parsed = PurePosixPath(path)
    return (
        not parsed.is_absolute()
        and ".." not in parsed.parts
        and str(parsed) == path
    )
