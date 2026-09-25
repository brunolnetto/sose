from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class StateConfiguration:
    """Canonical, hashable representation of an active statechart configuration.

    A flat FSM has one value. Hierarchical/parallel statecharts can expose several
    simultaneously active state ids (for example a compound parent plus its leaf,
    or one active leaf per parallel region).
    """

    states: tuple[str, ...]

    def __post_init__(self) -> None:
        normalized = tuple(sorted(dict.fromkeys(self.states)))
        if not normalized:
            raise ValueError("state configuration cannot be empty")
        object.__setattr__(self, "states", normalized)

    @classmethod
    def from_values(cls, values) -> "StateConfiguration":
        if isinstance(values, str):
            return cls((values,))
        return cls(tuple(str(value) for value in values))

    def contains(self, state_id: str) -> bool:
        return state_id in self.states


@dataclass(frozen=True, slots=True)
class TransitionEdge:
    """Static legal transition discovered from a statechart topology.

    `event=None` represents an eventless/automatic transition. Those transitions
    are part of the topology but are not sampled by the probabilistic selector;
    the statechart runtime remains responsible for automatic microsteps.
    """

    edge_id: str
    source: str
    targets: tuple[str, ...]
    event: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.edge_id:
            raise ValueError("edge_id cannot be empty")
        if not self.source:
            raise ValueError("edge source cannot be empty")
        object.__setattr__(self, "targets", tuple(str(x) for x in self.targets))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def available_from(self, configuration: StateConfiguration) -> bool:
        # A parent state appears in a hierarchical configuration together with its
        # active descendant, so membership naturally supports ancestor transitions.
        return configuration.contains(self.source)


@dataclass(frozen=True, slots=True)
class TransitionOption:
    """One probabilistic event option after legality/guard filtering."""

    event: str
    weight: float
    probability: float
    edges: tuple[TransitionEdge, ...]


@dataclass(frozen=True, slots=True)
class TransitionDecision:
    """Deterministically sampled event from a normalized distribution."""

    event: str
    weight: float
    probability: float
    draw: float
    configuration: StateConfiguration
    edges: tuple[TransitionEdge, ...]
