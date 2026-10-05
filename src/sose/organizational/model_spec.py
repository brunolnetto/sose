from __future__ import annotations

from copy import deepcopy
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    """Declarative, hash-addressed organizational experiment configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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
    agency: dict[str, object] = Field(default_factory=dict)
    parameters: dict[str, object] = Field(default_factory=dict)
    parameter_evidence: dict[str, EvidenceClass] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parameter_evidence(self) -> "ModelSpec":
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
        return self

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
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
    operating_cost: float = Field(default=0.0, ge=0.0)
    transition_cost: float = Field(default=0.0, ge=0.0)
    transition_time: float = Field(default=0.0, ge=0.0)

    def apply(self, spec: ModelSpec) -> tuple[ModelSpec, StructuralDiff]:
        payload = deepcopy(spec.canonical_payload())
        for path in self.remove_paths:
            _remove_path(payload, path)
        for path, value in self.set_values.items():
            _set_path(payload, path, deepcopy(value))
        transformed = ModelSpec.model_validate(payload)
        return transformed, spec.diff(transformed)


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
            child = _join(path, str(key))
            added[child] = after[key]
        for key in sorted(before_keys - after_keys):
            child = _join(path, str(key))
            removed[child] = before[key]
        for key in sorted(before_keys & after_keys):
            _diff_values(
                before[key],
                after[key],
                path=_join(path, str(key)),
                added=added,
                removed=removed,
                changed=changed,
            )
        return
    if before != after:
        changed[path] = (before, after)


def _join(parent: str, child: str) -> str:
    return child if not parent else f"{parent}.{child}"


def _set_path(payload: dict[str, Any], path: str, value: object) -> None:
    parts = _parts(path)
    current: dict[str, Any] = payload
    for part in parts[:-1]:
        child = current.get(part)
        if child is None:
            child = {}
            current[part] = child
        if not isinstance(child, dict):
            raise ValueError(f"cannot set {path!r}: {part!r} is not a mapping")
        current = child
    current[parts[-1]] = value


def _remove_path(payload: dict[str, Any], path: str) -> None:
    parts = _parts(path)
    current: dict[str, Any] = payload
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            raise ValueError(f"cannot remove {path!r}: parent does not exist")
        current = child
    if parts[-1] not in current:
        raise ValueError(f"cannot remove {path!r}: path does not exist")
    del current[parts[-1]]


def _parts(path: str) -> list[str]:
    parts = [part for part in path.split(".") if part]
    if not parts:
        raise ValueError("model path cannot be empty")
    return parts
