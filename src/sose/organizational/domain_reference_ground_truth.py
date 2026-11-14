"""Ground-truth claim models and canonical JSON helpers (split from domain_reference)."""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
from types import MappingProxyType
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    InstanceOf,
    StringConstraints,
    field_serializer,
    model_validator,
)


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


class GroundTruthComparisonKind(StrEnum):
    EXACT = "exact"
    ABSOLUTE_TOLERANCE = "absolute_tolerance"
    RELATIVE_TOLERANCE = "relative_tolerance"
    LOWER_BOUND = "lower_bound"
    UPPER_BOUND = "upper_bound"


class GroundTruthComparisonRule(BaseModel):
    """Explicit rule used to decide whether an eligible observation matches a claim.

    Semantics:
    - exact: observed equals expected;
    - absolute_tolerance: abs(observed - expected) <= tolerance;
    - relative_tolerance: abs(observed - expected) / abs(expected) <= tolerance;
    - lower_bound: observed >= expected;
    - upper_bound: observed <= expected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: GroundTruthComparisonKind
    tolerance: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_tolerance(self) -> "GroundTruthComparisonRule":
        tolerance_kinds = {
            GroundTruthComparisonKind.ABSOLUTE_TOLERANCE,
            GroundTruthComparisonKind.RELATIVE_TOLERANCE,
        }
        if self.kind in tolerance_kinds:
            if self.tolerance is None:
                raise ValueError(f"{self.kind.value} requires a positive tolerance")
        elif self.tolerance is not None:
            raise ValueError(f"{self.kind.value} does not accept tolerance")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "tolerance": self.tolerance,
        }


class GroundTruthClaim(BaseModel):
    """Typed, eligibility-aware reference claim for one configured world."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    claim_id: NonBlankString
    kind: GroundTruthKind
    target_kind: GroundTruthTargetKind
    target_name: NonBlankString
    expected: object
    comparison_rule: InstanceOf[GroundTruthComparisonRule]
    assumptions: tuple[NonBlankString, ...] = Field(min_length=1)
    eligibility_rule: NonBlankString
    eligible: bool
    ineligibility_reason: NonBlankString | None = None
    provenance: tuple[NonBlankString, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "GroundTruthClaim":
        _validate_json(self.expected, path="/expected")
        if self.comparison_rule.kind is not GroundTruthComparisonKind.EXACT:
            if not _is_finite_number(self.expected):
                raise ValueError(
                    "non-exact ground-truth comparison requires a finite numeric expected value"
                )
            if (
                self.comparison_rule.kind
                is GroundTruthComparisonKind.RELATIVE_TOLERANCE
                and float(self.expected) == 0.0
            ):
                raise ValueError(
                    "relative_tolerance ground-truth comparison requires non-zero expected value"
                )
        if self.eligible and self.ineligibility_reason is not None:
            raise ValueError("eligible ground-truth claim cannot have ineligibility_reason")
        if not self.eligible and self.ineligibility_reason is None:
            raise ValueError("ineligible ground-truth claim requires ineligibility_reason")
        _require_unique(self.assumptions, "ground-truth assumptions")
        _require_unique(self.provenance, "ground-truth provenance entries")
        object.__setattr__(self, "expected", _freeze_json(self.expected))
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "kind": self.kind.value,
            "target_kind": self.target_kind.value,
            "target_name": self.target_name,
            "expected": _thaw_json(self.expected),
            "comparison_rule": self.comparison_rule.canonical_payload(),
            "assumptions": sorted(self.assumptions),
            "eligibility_rule": self.eligibility_rule,
            "eligible": self.eligible,
            "ineligibility_reason": self.ineligibility_reason,
            "provenance": sorted(self.provenance),
        }

    @field_serializer("expected")
    def serialize_expected(self, value: object) -> object:
        return _thaw_json(value)

    @property
    def claim_hash(self) -> str:
        return _canonical_hash(self.canonical_payload())


def _require_unique(values, what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {what} are not allowed")


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


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
    )
