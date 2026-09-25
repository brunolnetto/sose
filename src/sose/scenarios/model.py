from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Protocol, TYPE_CHECKING

from sose.core.events import DomainEvent

if TYPE_CHECKING:
    from sose.core.context import SimulationContext


class ScenarioSignalKind(str, Enum):
    TICK = "tick"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class ScenarioSignal:
    kind: ScenarioSignalKind
    now: datetime
    tick: int
    event: DomainEvent | None = None


class ScenarioTrigger(Protocol):
    def matches(self, signal: ScenarioSignal) -> bool: ...
    def key(self, signal: ScenarioSignal) -> tuple[object, ...]: ...


@dataclass(frozen=True, slots=True)
class TickTrigger:
    every: int = 1
    offset: int = 0

    def __post_init__(self) -> None:
        if self.every < 1:
            raise ValueError("tick trigger every must be >= 1")
        if self.offset < 0:
            raise ValueError("tick trigger offset cannot be negative")

    def matches(self, signal: ScenarioSignal) -> bool:
        return (
            signal.kind is ScenarioSignalKind.TICK
            and signal.tick >= self.offset
            and (signal.tick - self.offset) % self.every == 0
        )

    def key(self, signal: ScenarioSignal) -> tuple[object, ...]:
        return ("tick", signal.tick)


@dataclass(frozen=True, slots=True)
class EventTrigger:
    event: str | None = None
    entity_type: str | None = None

    def matches(self, signal: ScenarioSignal) -> bool:
        if signal.kind is not ScenarioSignalKind.EVENT or signal.event is None:
            return False
        if self.event is not None and signal.event.name != self.event:
            return False
        if self.entity_type is not None and signal.event.entity_type != self.entity_type:
            return False
        return True

    def key(self, signal: ScenarioSignal) -> tuple[object, ...]:
        if signal.event is None:
            raise ValueError("event trigger requires an event signal")
        return ("event", signal.event.event_id)


@dataclass(frozen=True, slots=True)
class ScheduledTrigger:
    at: datetime

    def matches(self, signal: ScenarioSignal) -> bool:
        return signal.kind is ScenarioSignalKind.TICK and signal.now >= self.at

    def key(self, signal: ScenarioSignal) -> tuple[object, ...]:
        return ("scheduled", self.at)


@dataclass(frozen=True, slots=True)
class AttributeEffect:
    key: str
    value: Any

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("attribute effect key cannot be empty")


@dataclass(frozen=True, slots=True)
class TransitionWeightEffect:
    event: str
    multiplier: float
    entity_type: str | None = None

    def __post_init__(self) -> None:
        if not self.event:
            raise ValueError("transition weight effect event cannot be empty")
        multiplier = float(self.multiplier)
        if not math.isfinite(multiplier) or multiplier < 0:
            raise ValueError("transition weight multiplier must be finite and >= 0")
        object.__setattr__(self, "multiplier", multiplier)
        if self.entity_type == "":
            raise ValueError("entity_type cannot be empty")


@dataclass(frozen=True, slots=True)
class CompositeEffect:
    effects: tuple[ScenarioEffect, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "effects", tuple(self.effects))
        if not self.effects:
            raise ValueError("composite effect cannot be empty")


ScenarioEffect = AttributeEffect | TransitionWeightEffect | CompositeEffect


@dataclass(frozen=True, slots=True)
class ScenarioEvaluation:
    scenario: Scenario
    signal: ScenarioSignal
    context: SimulationContext


ScenarioCondition = Callable[[ScenarioEvaluation], bool]


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    trigger: ScenarioTrigger
    effects: tuple[ScenarioEffect, ...]
    condition: ScenarioCondition | None = None
    activation_probability: float = 1.0
    duration: timedelta | None = None
    priority: int = 100
    allow_reentry: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("scenario name cannot be empty")
        object.__setattr__(self, "effects", tuple(self.effects))
        if not self.effects:
            raise ValueError("scenario must define at least one effect")
        probability = float(self.activation_probability)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("activation_probability must be finite and between 0 and 1")
        object.__setattr__(self, "activation_probability", probability)
        if self.duration is not None and self.duration <= timedelta(0):
            raise ValueError("scenario duration must be positive")


@dataclass(frozen=True, slots=True)
class ScenarioDecision:
    attempt_id: str
    scenario_name: str
    activated: bool
    probability: float
    draw: float | None
    reason: str
    activation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScenarioActivation:
    activation_id: str
    scenario_name: str
    activated_at: datetime
    expires_at: datetime | None
    priority: int
    effects: tuple[ScenarioEffect, ...]
    trigger_key: tuple[object, ...]
    causation_id: str | None = None
    correlation_id: str | None = None


def iter_leaf_effects(effects: tuple[ScenarioEffect, ...]):
    for effect in effects:
        if isinstance(effect, CompositeEffect):
            yield from iter_leaf_effects(effect.effects)
        else:
            yield effect
