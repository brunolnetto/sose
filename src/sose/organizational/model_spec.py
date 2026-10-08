from __future__ import annotations

from copy import deepcopy
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agency import AgencySpec


class EvidenceClass(StrEnum):
    OBSERVED = "observed"
    INFERABLE = "inferable"
    ASSUMED = "assumed"


class InterventionClass(StrEnum):
    CAPACITY = "capacity"
    POLICY = "policy"
    AUTOMATION = "automation"
    STRUCTURAL = "structural"
    ADAPTIVE_MANAGERIAL = "adaptive_managerial"


class StructuralDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    added: dict[str, object] = Field(default_factory=dict)
    removed: dict[str, object] = Field(default_factory=dict)
    changed: dict[str, tuple[object, object]] = Field(default_factory=dict)


class ModelSpec(BaseModel):
    """Declarative, deeply immutable, hash-addressed experiment configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    version: str = "1"
    stations: dict[str, object] = Field(default_factory=dict)
    actors: dict[str, object] = Field(default_factory=dict)
    capabilities: dict[str, object] = Field(default_factory=dict)
    calendars: dict[str, object] = Field(default_factory=dict)
    routing: dict[str, object] = Field(default_factory=dict)
    authority: dict[str, object] = Field(default_factory=dict)
    quality_gates: dict[str, object] = Field(default_factory=dict)
    policies: dict[str, object] = Field(default_factory=dict)
    demand: dict[str, object] = Field(default_factory=dict)
    costs: dict[str, object] = Field(default_factory=dict)
    agency: AgencySpec = Field(default_factory=AgencySpec)
    parameters: dict[str, object] = Field(default_factory=dict)
    parameter_evidence: dict[str, EvidenceClass] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "ModelSpec":
        missing = sorted(set(self.parameters) - set(self.parameter_evidence))
        if missing:
            raise ValueError(
                "every declared parameter requires an evidence class; missing: "
                + ", ".join(missing)
            )
        unknown = sorted(set(self.parameter_evidence) - set(self.parameters))
        if unknown:
            raise ValueError(
                "parameter_evidence contains undeclared parameters: "
                + ", ".join(unknown)
            )
        for name in _JSON_MAPPING_FIELDS:
            value = getattr(self, name)
            _validate_json(value, path=f"/{name}")
            object.__setattr__(self, name, _freeze(value))
        object.__setattr__(
            self,
            "parameter_evidence",
            MappingProxyType(dict(self.parameter_evidence)),
        )
        return self

    def canonical_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"version": self.version}
        for name in _JSON_MAPPING_FIELDS:
            payload[name] = _thaw(getattr(self, name))
        payload["agency"] = self.agency.canonical_payload()
        payload["parameter_evidence"] = {
            key: value.value for key, value in self.parameter_evidence.items()
        }
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
    def model_spec_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def diff(self, other: "ModelSpec") -> StructuralDiff:
        added: dict[str, object] = {}
        removed: dict[str, object] = {}
        changed: dict[str, tuple[object, object]] = {}
        _diff_values(
            self.canonical_payload(),
            other.canonical_payload(),
            path="",
            added=added,
            removed=removed,
            changed=changed,
        )
        return StructuralDiff(added=added, removed=removed, changed=changed)


_JSON_MAPPING_FIELDS = (
    "stations",
    "actors",
    "capabilities",
    "calendars",
    "routing",
    "authority",
    "quality_gates",
    "policies",
    "demand",
    "costs",
    "parameters",
)
_MISSING = object()


class ModelIntervention(BaseModel):
    """Named, auditable transformation from one ModelSpec to another."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intervention_id: str
    intervention_class: InterventionClass
    set_values: dict[str, object] = Field(default_factory=dict)
    remove_paths: list[str] = Field(default_factory=list)
    mechanisms_added: list[str] = Field(default_factory=list)
    mechanisms_removed: list[str] = Field(default_factory=list)
    mechanisms_changed: list[str] = Field(default_factory=list)
    operating_cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    transition_cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    transition_time: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "intervention_id": self.intervention_id,
            "intervention_class": self.intervention_class.value,
            "set_values": deepcopy(self.set_values),
            "remove_paths": list(self.remove_paths),
            "mechanisms_added": list(self.mechanisms_added),
            "mechanisms_removed": list(self.mechanisms_removed),
            "mechanisms_changed": list(self.mechanisms_changed),
            "operating_cost": self.operating_cost,
            "transition_cost": self.transition_cost,
            "transition_time": self.transition_time,
        }

    @property
    def intervention_hash(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(payload).hexdigest()

    def apply(self, spec: ModelSpec) -> tuple[ModelSpec, StructuralDiff]:
        payload = deepcopy(spec.canonical_payload())
        for path in self.remove_paths:
            _remove_path(payload, path)
        for path, value in self.set_values.items():
            _set_path(payload, path, deepcopy(value))
        transformed = ModelSpec.model_validate(payload)
        return transformed, spec.diff(transformed)


def _validate_json(value: object, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"JSON object key at {path} must be a string")
            _validate_json(child, path=_join_pointer(path, key))
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_json(child, path=_join_pointer(path, str(index)))
        return
    raise ValueError(f"value at {path} is not JSON-compatible: {type(value).__name__}")


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, MappingProxyType):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, dict):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    if isinstance(value, EvidenceClass):
        return value.value
    return value


def _diff_values(
    before: object,
    after: object,
    *,
    path: str,
    added: dict[str, object],
    removed: dict[str, object],
    changed: dict[str, tuple[object, object]],
) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(after_keys - before_keys):
            child = _join_pointer(path, str(key))
            added[child] = after[key]
        for key in sorted(before_keys - after_keys):
            child = _join_pointer(path, str(key))
            removed[child] = before[key]
        for key in sorted(before_keys & after_keys):
            _diff_values(
                before[key],
                after[key],
                path=_join_pointer(path, str(key)),
                added=added,
                removed=removed,
                changed=changed,
            )
        return
    if not _same_json_value(before, after):
        changed[path] = (before, after)


def _same_json_value(before: object, after: object) -> bool:
    if type(before) is not type(after):
        return False
    if isinstance(before, list) and isinstance(after, list):
        return len(before) == len(after) and all(
            _same_json_value(left, right)
            for left, right in zip(before, after, strict=True)
        )
    return before == after


def _join_pointer(parent: str, child: str) -> str:
    escaped = child.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}" if parent else f"/{escaped}"


def _set_path(payload: dict[str, Any], path: str, value: object) -> None:
    parts = _pointer_parts(path)
    current: dict[str, Any] = payload
    for part in parts[:-1]:
        child = current.get(part, _MISSING)
        if child is _MISSING:
            child = {}
            current[part] = child
        if not isinstance(child, dict):
            raise ValueError(f"cannot set {path!r}: {part!r} is not a mapping")
        current = child
    current[parts[-1]] = value


def _remove_path(payload: dict[str, Any], path: str) -> None:
    parts = _pointer_parts(path)
    current: dict[str, Any] = payload
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            raise ValueError(f"cannot remove {path!r}: parent does not exist")
        current = child
    if parts[-1] not in current:
        raise ValueError(f"cannot remove {path!r}: path does not exist")
    del current[parts[-1]]


def _pointer_parts(path: str) -> list[str]:
    if not path.startswith("/") or path == "/":
        raise ValueError("model path must be a non-root JSON Pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
