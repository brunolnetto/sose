from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, InstanceOf, StringConstraints, model_validator

from .agency import AgencyLevel
from .experiment import ParameterRange
from .model_spec import EvidenceClass


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class GroundTruthKind(StrEnum):
    ANALYTICAL = "analytical"
    MECHANISTIC = "mechanistic"
    INVARIANT = "invariant"
    REFERENCE_SIMULATION = "reference_simulation"
    EMPIRICAL = "empirical"


class GroundTruthTargetKind(StrEnum):
    METRIC = "metric"
    REGIME = "regime"
    INVARIANT = "invariant"
    RELATION = "relation"


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

        for name, value in self.defaults.items():
            if not isfinite(value):
                raise ValueError(f"agency capability default for {name} must be finite")
            bounds = self.parameter_ranges[name]
            if not bounds.low <= value <= bounds.high:
                raise ValueError(
                    f"agency capability default for {name} must lie within its range"
                )

        if self.level is AgencyLevel.A2:
            if not self.observation_contract_ids or not self.action_contract_ids:
                raise ValueError("A2 requires observation and action contracts")
        elif self.observation_contract_ids or self.action_contract_ids:
            raise ValueError("observation/action contracts are only valid for A2")

        if len(set(self.compatibility_constraints)) != len(
            self.compatibility_constraints
        ):
            raise ValueError("duplicate agency compatibility constraints are not allowed")
        if len(set(self.observation_contract_ids)) != len(
            self.observation_contract_ids
        ):
            raise ValueError("duplicate observation contract ids are not allowed")
        if len(set(self.action_contract_ids)) != len(self.action_contract_ids):
            raise ValueError("duplicate action contract ids are not allowed")

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

    @property
    def capability_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


class GroundTruthClaim(BaseModel):
    """Typed, eligibility-aware reference claim for one configured world."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    claim_id: NonBlankString
    kind: GroundTruthKind
    target_kind: GroundTruthTargetKind
    target_name: NonBlankString
    expected: object
    assumptions: tuple[NonBlankString, ...] = Field(min_length=1)
    eligibility_rule: NonBlankString
    eligible: bool
    ineligibility_reason: NonBlankString | None = None
    provenance: tuple[NonBlankString, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "GroundTruthClaim":
        _validate_json(self.expected, path="/expected")
        if self.eligible and self.ineligibility_reason is not None:
            raise ValueError("eligible ground-truth claim cannot have ineligibility_reason")
        if not self.eligible and self.ineligibility_reason is None:
            raise ValueError("ineligible ground-truth claim requires ineligibility_reason")
        if len(set(self.assumptions)) != len(self.assumptions):
            raise ValueError("duplicate ground-truth assumptions are not allowed")
        if len(set(self.provenance)) != len(self.provenance):
            raise ValueError("duplicate ground-truth provenance entries are not allowed")
        object.__setattr__(self, "expected", _freeze_json(self.expected))
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "kind": self.kind.value,
            "target_kind": self.target_kind.value,
            "target_name": self.target_name,
            "expected": _thaw_json(self.expected),
            "assumptions": sorted(self.assumptions),
            "eligibility_rule": self.eligibility_rule,
            "eligible": self.eligible,
            "ineligibility_reason": self.ineligibility_reason,
            "provenance": sorted(self.provenance),
        }

    @property
    def claim_hash(self) -> str:
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
        parameter_names = [item.name for item in self.parameters]
        if len(set(parameter_names)) != len(parameter_names):
            raise ValueError("duplicate parameter names are not allowed")

        capability_keys = [
            (item.level.value, item.capability_id)
            for item in self.agency_capabilities
        ]
        if len(set(capability_keys)) != len(capability_keys):
            raise ValueError("duplicate agency capability declarations are not allowed")

        if len(set(self.ground_truth_kinds)) != len(self.ground_truth_kinds):
            raise ValueError("duplicate ground-truth kinds are not allowed")
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


def _is_repository_relative(path: str) -> bool:
    if not path or "\\" in path:
        return False
    parsed = PurePosixPath(path)
    return (
        not parsed.is_absolute()
        and ".." not in parsed.parts
        and str(parsed) == path
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
