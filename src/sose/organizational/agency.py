from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgencyLevel(StrEnum):
    A0 = "A0"
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"


class A1PolicyName(StrEnum):
    QUEUE_BYPASS = "queue_bypass"
    PRIORITY_INFLATION = "priority_inflation"
    BATCHING = "batching"
    INFORMAL_CHANNEL = "informal_channel"
    DEFERRAL = "deferral"


class A1Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: A1PolicyName
    parameters: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_and_freeze_parameters(self) -> "A1Policy":
        _validate_json(self.parameters, path="/parameters")
        object.__setattr__(self, "parameters", _freeze(self.parameters))
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {"name": self.name.value, "parameters": _thaw(self.parameters)}


class A2ObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metrics: tuple[NonBlankString, ...] = Field(min_length=1)
    aggregation_window: float = Field(gt=0.0, allow_inf_nan=False)
    observation_delay: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    measurement_noise_std: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    trigger_rule: NonBlankString
    cooldown: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    permitted_actions: tuple[NonBlankString, ...] = Field(min_length=1)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "metrics": list(self.metrics),
            "aggregation_window": self.aggregation_window,
            "observation_delay": self.observation_delay,
            "measurement_noise_std": self.measurement_noise_std,
            "trigger_rule": self.trigger_rule,
            "cooldown": self.cooldown,
            "permitted_actions": list(self.permitted_actions),
        }


class AgencySpec(BaseModel):
    """Experiment-level agency contract; A3 is an explicit model boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: AgencyLevel = AgencyLevel.A0
    a1_policy: A1Policy | None = None
    a2_observation: A2ObservationModel | None = None

    @model_validator(mode="after")
    def validate_level_contract(self) -> "AgencySpec":
        if self.level is AgencyLevel.A3:
            raise ValueError("A3 is outside the organizational simulation model")
        if self.level is AgencyLevel.A1:
            if self.a1_policy is None:
                raise ValueError("A1 requires a named adaptive policy")
            if self.a2_observation is not None:
                raise ValueError("A2 observation model is only valid for A2")
            return self
        if self.a1_policy is not None:
            raise ValueError("A1 policy is only valid for A1")
        if self.level is AgencyLevel.A2:
            if self.a2_observation is None:
                raise ValueError("A2 requires an observation model")
            return self
        if self.a2_observation is not None:
            raise ValueError("A2 observation model is only valid for A2")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "level": self.level.value,
            "a1_policy": None if self.a1_policy is None else self.a1_policy.canonical_payload(),
            "a2_observation": (
                None if self.a2_observation is None else self.a2_observation.canonical_payload()
            ),
        }


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


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value
