from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sose.core.identity import deterministic_id

from .graph import ProbabilisticTransitionGraph
from .model import TransitionEdge


@dataclass(slots=True)
class TransitionGraphBuilder:
    """Small explicit builder used by domain packs and StateChart adapters."""

    name: str
    _edges: list[TransitionEdge] = field(default_factory=list)

    def edge(
        self,
        source: str,
        event: str | None,
        target: str | None = None,
        *additional_targets: str,
        edge_id: str | None = None,
        **metadata: Any,
    ) -> "TransitionGraphBuilder":
        targets = (() if target is None else (target,)) + additional_targets
        resolved = edge_id or deterministic_id(
            "transition-edge",
            self.name,
            source,
            event or "<eventless>",
            *targets,
            len(self._edges),
        )
        self._edges.append(
            TransitionEdge(
                edge_id=resolved,
                source=source,
                targets=targets,
                event=event,
                metadata=metadata,
            )
        )
        return self

    def build(self) -> ProbabilisticTransitionGraph:
        return ProbabilisticTransitionGraph(self._edges)
