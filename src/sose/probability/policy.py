from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, TYPE_CHECKING

from sose.domain.entity import Entity

from .model import StateConfiguration, TransitionEdge

if TYPE_CHECKING:
    from sose.core.context import SimulationContext


@dataclass(frozen=True, slots=True)
class TransitionEvaluation:
    """Context supplied to dynamic transition-weight functions."""

    event: str
    configuration: StateConfiguration
    edges: tuple[TransitionEdge, ...]
    entity: Entity
    context: "SimulationContext"


class WeightPolicy(Protocol):
    def weight(self, evaluation: TransitionEvaluation) -> float: ...


@dataclass(frozen=True, slots=True)
class ConstantWeight:
    value: float

    def weight(self, evaluation: TransitionEvaluation) -> float:
        return float(self.value)


@dataclass(frozen=True, slots=True)
class CallableWeight:
    fn: Callable[[TransitionEvaluation], float]

    def weight(self, evaluation: TransitionEvaluation) -> float:
        return float(self.fn(evaluation))


WeightSpec = float | int | WeightPolicy | Callable[[TransitionEvaluation], float]


class TransitionPolicy:
    """Stochastic semantics attached to statechart event choices.

    Weights are intentionally not probabilities: SOSE normalizes them *after*
    statechart legality/guard filtering. This is essential when guards disable one
    branch of a fork.
    """

    def __init__(
        self,
        weights: Mapping[str, WeightSpec] | None = None,
        *,
        default_weight: WeightSpec = 1.0,
        strict: bool = False,
    ) -> None:
        self._weights = dict(weights or {})
        self._default_weight = default_weight
        self.strict = strict

    def has_explicit_weight(self, event: str) -> bool:
        return event in self._weights

    def weight_for(self, evaluation: TransitionEvaluation) -> float:
        if self.strict and evaluation.event not in self._weights:
            raise KeyError(f"no transition weight configured for event: {evaluation.event}")
        spec = self._weights.get(evaluation.event, self._default_weight)
        return _resolve(spec, evaluation)

    @property
    def events(self) -> tuple[str, ...]:
        return tuple(self._weights)


def probabilistic(
    weights: Mapping[str, WeightSpec],
    *,
    default_weight: WeightSpec = 1.0,
    strict: bool = True,
) -> TransitionPolicy:
    """Concise domain-facing constructor for a transition policy."""

    return TransitionPolicy(weights, default_weight=default_weight, strict=strict)


def _resolve(spec: WeightSpec, evaluation: TransitionEvaluation) -> float:
    if isinstance(spec, (int, float)):
        return float(spec)
    weight_method = getattr(spec, "weight", None)
    if callable(weight_method):
        return float(weight_method(evaluation))
    if callable(spec):
        return float(spec(evaluation))
    raise TypeError(f"unsupported transition weight: {spec!r}")


def probabilistic_transitions(
    weights: Mapping[str, WeightSpec],
    *,
    default_weight: WeightSpec = 1.0,
    strict: bool = False,
):
    """Attach stochastic semantics to a StateChart class without altering its FSM API."""

    policy = TransitionPolicy(weights, default_weight=default_weight, strict=strict)

    def decorate(chart_cls):
        chart_cls.sose_transition_policy = policy
        return chart_cls

    return decorate
