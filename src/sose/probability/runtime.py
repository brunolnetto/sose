from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Callable

from sose.core.randomness import RandomSource
from sose.domain.entity import Entity

from .graph import ProbabilisticTransitionGraph
from .model import StateConfiguration, TransitionDecision
from .policy import TransitionPolicy


class ProbabilisticTransitionRuntime:
    """Deterministic executor for stochastic choices over legal StateChart events."""

    def __init__(
        self,
        *,
        random_source: RandomSource,
        tick: Callable[[], int],
    ) -> None:
        self._random = random_source
        self._tick = tick

    def decide(
        self,
        *,
        chart,
        graph: ProbabilisticTransitionGraph,
        policy: TransitionPolicy,
        entity: Entity,
        context,
        guard_kwargs: Mapping[str, Any] | None = None,
        scope: tuple[object, ...] = (),
    ) -> TransitionDecision:
        configuration = StateConfiguration.from_values(_configuration_values(chart))
        enabled = _enabled_events(chart, guard_kwargs or {})
        distribution = graph.distribution(
            configuration=configuration,
            policy=policy,
            entity=entity,
            context=context,
            enabled_events=enabled,
        )
        rng = self._random.for_scope(
            "transition",
            self._tick(),
            entity.entity_type,
            entity.id,
            *configuration.states,
            *scope,
        )
        return graph.sample(distribution, rng=rng, configuration=configuration)


def _configuration_values(chart) -> Iterable[str]:
    values = getattr(chart, "configuration_values", None)
    if values is None:
        current = getattr(chart, "current_state_value", None)
        if current is None:
            raise TypeError("chart must expose configuration_values or current_state_value")
        return (str(current),)
    return tuple(str(value) for value in values)


def _enabled_events(chart, kwargs: Mapping[str, Any]) -> set[str] | None:
    fn = getattr(chart, "enabled_events", None)
    if not callable(fn):
        # None means graph topology is the only legality filter. This is useful for
        # tests/adapters, but a real StateChart should normally expose enabled_events().
        return None
    values = fn(**dict(kwargs))
    return {_event_name(value) for value in values}


def _event_name(value) -> str:
    for attr in ("id", "name"):
        result = getattr(value, attr, None)
        if result:
            return str(result)
    return str(value)
