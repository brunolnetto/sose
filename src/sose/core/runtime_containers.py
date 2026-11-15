"""Durable Container record types and validators, split from ``sose.core.runtime``."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


def _require_non_empty(value: str, *, label: str) -> None:
    if not value:
        raise ValueError(f"{label} cannot be empty")


def _require_positive_finite(value: float, *, label: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and > 0")


def _require_nonnegative_finite(value: float, *, label: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and >= 0")


def _require_container_level(value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError("container levels must be finite and >= 0")


@dataclass(frozen=True, slots=True)
class ContainerDefinition:
    name: str
    capacity: float
    initial: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("container name cannot be empty")
        if not math.isfinite(self.capacity) or self.capacity <= 0:
            raise ValueError("container capacity must be finite and > 0")
        if (
            not math.isfinite(self.initial)
            or self.initial < 0
            or self.initial > self.capacity
        ):
            raise ValueError(
                "container initial level must be finite and between 0 and capacity"
            )


@dataclass(frozen=True, slots=True)
class ContainerState:
    name: str
    level: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("container name cannot be empty")
        if not math.isfinite(self.level) or self.level < 0:
            raise ValueError("container level must be finite and >= 0")


@dataclass(frozen=True, slots=True)
class ContainerOperationIntent:
    request_id: str
    container_name: str
    operation: str
    amount: float
    requested_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.container_name:
            raise ValueError("container_name cannot be empty")
        if self.operation not in {"put", "get"}:
            raise ValueError("container operation must be put or get")
        if not math.isfinite(self.amount) or self.amount <= 0:
            raise ValueError("container amount must be finite and > 0")
        if self.sequence < 1:
            raise ValueError("container operation sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class ContainerOperationResult:
    request_id: str
    container_name: str
    operation: str
    amount: float
    completed_at: datetime
    level_before: float
    level_after: float
    sequence: int

    def __post_init__(self) -> None:
        _require_non_empty(self.request_id, label="request_id")
        _require_non_empty(self.container_name, label="container_name")
        if self.operation not in {"put", "get"}:
            raise ValueError("container operation must be put or get")
        _require_positive_finite(self.amount, label="container amount")
        _require_container_level(self.level_before)
        _require_container_level(self.level_after)
        if self.sequence < 1:
            raise ValueError("container result sequence must be >= 1")


# Defined in ``sose.core.runtime`` before the split and still re-exported from it.
# ``__module__`` stays pinned to that public home so class paths (codec, pickle,
# reprs) are unchanged by the move.
for _moved in (
    ContainerDefinition,
    ContainerState,
    ContainerOperationIntent,
    ContainerOperationResult,
):
    _moved.__module__ = "sose.core.runtime"
del _moved
