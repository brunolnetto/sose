from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sose._deprecation import warn_deprecated
from sose.core.context import SimulationContext
from sose.domain.entity import Entity

_INSTEAD = "model external interventions with Scenario and ScenarioEngine"

Predicate = Callable[[Entity, SimulationContext], bool]
Decision = Callable[[Entity, SimulationContext], str | None]


@dataclass(frozen=True, slots=True)
class ScenarioRule:
    """Legacy v0.3 event-selection rule, deprecated in 0.11.0 and removed in 0.12.0.

    New external interventions should use ``Scenario`` and ``ScenarioEngine``.
    """

    name: str
    when: Predicate
    decide: Decision

    def __post_init__(self) -> None:
        # helper -> __post_init__ -> dataclass __init__ -> constructing caller
        warn_deprecated("ScenarioRule", since="0.11.0", removed_in="0.12.0",
                        instead=_INSTEAD, stacklevel=4)

    def evaluate(self, entity: Entity, context: SimulationContext) -> str | None:
        if not self.when(entity, context):
            return None
        return self.decide(entity, context)


def warn_rule_hook(what: str) -> None:
    """Deprecation for the Engine's ScenarioRule hook (``rules_for``/``evaluate_entity``)."""
    # helper -> Engine method -> calling code
    warn_deprecated(what, since="0.11.0", removed_in="0.12.0", instead=_INSTEAD, stacklevel=4)
