from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sose.core.context import SimulationContext
from sose.domain.entity import Entity

Predicate = Callable[[Entity, SimulationContext], bool]
Decision = Callable[[Entity, SimulationContext], str | None]


@dataclass(frozen=True, slots=True)
class ScenarioRule:
    """Chooses a valid statechart event; it never mutates entity state directly."""

    name: str
    when: Predicate
    decide: Decision

    def evaluate(self, entity: Entity, context: SimulationContext) -> str | None:
        if not self.when(entity, context):
            return None
        return self.decide(entity, context)
