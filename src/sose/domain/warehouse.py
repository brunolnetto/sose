"""Domain warehouse boundary for the simulated business world.

EnginePersistence answers "how does execution resume?".  DomainWarehouse answers
"what is the current simulated business state?".  The two contracts are
intentionally independent even when a deployment chooses the same database
technology for both.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

from sose.domain.entity import Entity


@dataclass(frozen=True, slots=True)
class DomainMutation:
    """Idempotent business-state mutation prepared by the engine."""

    mutation_id: str
    entity: Entity

    def __post_init__(self) -> None:
        if not self.mutation_id:
            raise ValueError("mutation_id cannot be empty")


class DomainWarehouse(Protocol):
    """Durable current-state boundary for domain entities."""

    def entity(self, entity_type: str, entity_id: str) -> Entity | None: ...
    def entities(self, entity_type: str | None = None) -> tuple[Entity, ...]: ...
    def apply(self, mutation: DomainMutation) -> bool:
        """Apply once; return False for an identical replay."""


class MemoryDomainWarehouse:
    """Reference implementation used to prove warehouse semantics."""

    def __init__(self) -> None:
        self._entities: dict[tuple[str, str], Entity] = {}
        self._mutations: dict[str, DomainMutation] = {}

    def entity(self, entity_type: str, entity_id: str) -> Entity | None:
        value = self._entities.get((entity_type, entity_id))
        return deepcopy(value) if value is not None else None

    def entities(self, entity_type: str | None = None) -> tuple[Entity, ...]:
        values = (
            value
            for (kind, _), value in self._entities.items()
            if entity_type is None or kind == entity_type
        )
        return tuple(
            deepcopy(value)
            for value in sorted(values, key=lambda item: (item.entity_type, item.id))
        )

    def apply(self, mutation: DomainMutation) -> bool:
        existing = self._mutations.get(mutation.mutation_id)
        if existing is not None:
            if existing != mutation:
                raise ValueError(
                    f"domain mutation identity conflict: {mutation.mutation_id}"
                )
            return False
        key = (mutation.entity.entity_type, mutation.entity.id)
        self._entities[key] = deepcopy(mutation.entity)
        self._mutations[mutation.mutation_id] = deepcopy(mutation)
        return True

    def mutation(self, mutation_id: str) -> DomainMutation | None:
        value = self._mutations.get(mutation_id)
        return deepcopy(value) if value is not None else None
