from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable

from sose.domain.entity import Entity

from .model import (
    StateConfiguration,
    TransitionDecision,
    TransitionEdge,
    TransitionOption,
)
from .policy import TransitionEvaluation, TransitionPolicy


class NoProbabilisticTransition(RuntimeError):
    pass


class ProbabilisticTransitionGraph:
    """Legal statechart topology plus stochastic event semantics.

    The graph never decides whether a statechart guard is valid by itself. The
    caller supplies `enabled_events`, normally obtained from the bound StateChart,
    so guards/validators remain owned by the statechart runtime.
    """

    def __init__(self, edges: Iterable[TransitionEdge]) -> None:
        self._edges = tuple(edges)
        if not self._edges:
            raise ValueError("probabilistic transition graph needs at least one edge")
        ids = [edge.edge_id for edge in self._edges]
        if len(ids) != len(set(ids)):
            raise ValueError("transition edge ids must be unique")

    @property
    def edges(self) -> tuple[TransitionEdge, ...]:
        return self._edges

    def outgoing(self, configuration: StateConfiguration) -> tuple[TransitionEdge, ...]:
        return tuple(edge for edge in self._edges if edge.available_from(configuration))

    def event_edges(
        self,
        configuration: StateConfiguration,
        *,
        enabled_events: Iterable[str] | None = None,
    ) -> dict[str, tuple[TransitionEdge, ...]]:
        allowed = None if enabled_events is None else {str(event) for event in enabled_events}
        grouped: dict[str, list[TransitionEdge]] = defaultdict(list)
        for edge in self.outgoing(configuration):
            if edge.event is None:
                # Eventless transitions stay under StateChart/SCXML control.
                continue
            if allowed is not None and edge.event not in allowed:
                continue
            grouped[edge.event].append(edge)
        return {event: tuple(edges) for event, edges in grouped.items()}

    def distribution(
        self,
        *,
        configuration: StateConfiguration,
        policy: TransitionPolicy,
        entity: Entity,
        context,
        enabled_events: Iterable[str] | None = None,
        weight_transform: Callable[[TransitionEvaluation, float], float] | None = None,
    ) -> tuple[TransitionOption, ...]:
        grouped = self.event_edges(configuration, enabled_events=enabled_events)
        weighted: list[tuple[str, float, tuple[TransitionEdge, ...]]] = []

        for event in sorted(grouped):
            edges = grouped[event]
            evaluation = TransitionEvaluation(
                event=event,
                configuration=configuration,
                edges=edges,
                entity=entity,
                context=context,
            )
            weight = policy.weight_for(evaluation)
            if weight_transform is not None:
                weight = float(weight_transform(evaluation, weight))
            if not math.isfinite(weight):
                raise ValueError(f"transition weight for {event!r} must be finite")
            if weight < 0:
                raise ValueError(f"transition weight for {event!r} cannot be negative")
            if weight > 0:
                weighted.append((event, weight, edges))

        total = sum(weight for _, weight, _ in weighted)
        if total <= 0:
            raise NoProbabilisticTransition(
                f"no positive-weight enabled transition from {configuration.states}"
            )

        return tuple(
            TransitionOption(
                event=event,
                weight=weight,
                probability=weight / total,
                edges=edges,
            )
            for event, weight, edges in weighted
        )

    def sample(
        self,
        distribution: tuple[TransitionOption, ...],
        *,
        rng: random.Random,
        configuration: StateConfiguration,
    ) -> TransitionDecision:
        if not distribution:
            raise NoProbabilisticTransition("cannot sample an empty distribution")

        draw = rng.random()
        cumulative = 0.0
        selected = distribution[-1]
        for option in distribution:
            cumulative += option.probability
            if draw < cumulative:
                selected = option
                break

        return TransitionDecision(
            event=selected.event,
            weight=selected.weight,
            probability=selected.probability,
            draw=draw,
            configuration=configuration,
            edges=selected.edges,
        )
