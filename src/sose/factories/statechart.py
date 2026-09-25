from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Mapping

from sose.core.events import Command, DomainEvent
from sose.domain.entity import Entity
from sose.domain.registry import DomainRegistry
from sose.probability.model import TransitionDecision
from sose.probability.runtime import ProbabilisticTransitionRuntime
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart

from .event import EventFactory

if TYPE_CHECKING:
    from sose.core.context import SimulationContext
    from sose.statecharts.base import TransitionRecorder

Cause = Command | DomainEvent


class StateChartFactory:
    """Owns StateChart runtime binding and SOSE stochastic projection.

    StateChart classes remain domain code. SOSE extracts their legal topology,
    attaches transition listeners, and overlays probabilistic semantics without
    forking or modifying python-statemachine.
    """

    def __init__(
        self,
        *,
        registry: DomainRegistry,
        events: EventFactory,
        emit: Callable[[DomainEvent], None],
        now: Callable[[], datetime],
        transition_runtime: ProbabilisticTransitionRuntime,
        context: Callable[[], "SimulationContext"],
    ) -> None:
        self._registry = registry
        self._events = events
        self._emit = emit
        self._now = now
        self._transition_runtime = transition_runtime
        self._context = context
        self._graphs: dict[type, Any] = {}

    def bind(self, entity: Entity, *, caused_by: Cause | None = None):
        from sose.statecharts.base import TransitionRecorder, attach_recorder

        chart = self._registry.chart_for(entity)
        attach_recorder(
            chart,
            TransitionRecorder(
                entity=entity,
                now=self._now,
                emit=self._emit,
                events=self._events,
                caused_by=caused_by,
            ),
        )
        return chart

    def graph(self, chart):
        chart_type = type(chart)
        graph = self._graphs.get(chart_type)
        if graph is None:
            graph = graph_from_statechart(chart)
            self._graphs[chart_type] = graph
        return graph

    def policy(self, chart):
        return policy_from_statechart(chart)

    def decide(
        self,
        entity: Entity,
        *,
        guard_kwargs: Mapping[str, Any] | None = None,
        scope: tuple[object, ...] = (),
    ) -> TransitionDecision:
        """Choose one currently enabled event without mutating the statechart."""

        chart = self.bind(entity)
        return self._transition_runtime.decide(
            chart=chart,
            graph=self.graph(chart),
            policy=self.policy(chart),
            entity=entity,
            context=self._context(),
            guard_kwargs=guard_kwargs,
            scope=scope,
        )
