from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sose.core.identity import deterministic_id
from sose.probability.graph import ProbabilisticTransitionGraph
from sose.probability.model import TransitionEdge
from sose.probability.policy import TransitionPolicy


def graph_from_statechart(chart) -> ProbabilisticTransitionGraph:
    """Extract legal topology from the public python-statemachine model surface.

    The adapter intentionally uses State.transitions plus Transition.source,
    Transition.targets and Transition.events. It does not evaluate guards. Guards
    remain runtime semantics and are applied later through StateChart.enabled_events().
    """

    states = getattr(chart, "states", None)
    if states is None:
        raise TypeError("statechart must expose a public states collection")

    edges: list[TransitionEdge] = []
    seen_transition_ids: set[int] = set()
    ordinal = 0

    for state in _walk_states(states):
        transitions = getattr(state, "transitions", ())
        for transition in transitions:
            identity = id(transition)
            if identity in seen_transition_ids:
                continue
            seen_transition_ids.add(identity)

            source = _state_id(getattr(transition, "source", state))
            targets = tuple(_state_id(target) for target in getattr(transition, "targets", ()))
            events = tuple(_event_id(event) for event in getattr(transition, "events", ()))
            metadata = {
                "internal": bool(getattr(transition, "internal", False)),
                "initial": bool(getattr(transition, "initial", False)),
            }

            if not events:
                edges.append(
                    TransitionEdge(
                        edge_id=_edge_id(type(chart).__name__, source, None, targets, ordinal),
                        source=source,
                        targets=targets,
                        event=None,
                        metadata=metadata,
                    )
                )
                ordinal += 1
                continue

            for event in events:
                edges.append(
                    TransitionEdge(
                        edge_id=_edge_id(type(chart).__name__, source, event, targets, ordinal),
                        source=source,
                        targets=targets,
                        event=event,
                        metadata=metadata,
                    )
                )
                ordinal += 1

    if not edges:
        raise ValueError("statechart exposes no transitions")
    return ProbabilisticTransitionGraph(edges)


def policy_from_statechart(chart) -> TransitionPolicy:
    """Return SOSE stochastic semantics attached to a StateChart class.

    A chart without explicit semantics remains usable; outgoing enabled events have
    equal default weight.
    """

    policy = getattr(type(chart), "sose_transition_policy", None)
    if policy is None:
        return TransitionPolicy()
    if not isinstance(policy, TransitionPolicy):
        raise TypeError("sose_transition_policy must be a TransitionPolicy")
    return policy


def _walk_states(states: Iterable[Any]):
    for state in states:
        yield state
        children = getattr(state, "states", ())
        if children:
            yield from _walk_states(children)


def _state_id(state) -> str:
    value = getattr(state, "id", None)
    if value is None:
        raise TypeError(f"state without id: {state!r}")
    return str(value)


def _event_id(event) -> str:
    value = getattr(event, "id", None)
    return str(value if value is not None else event)


def _edge_id(
    chart_name: str,
    source: str,
    event: str | None,
    targets: tuple[str, ...],
    ordinal: int,
) -> str:
    return deterministic_id(
        "transition-edge",
        chart_name,
        source,
        event or "<eventless>",
        *targets,
        ordinal,
    )
