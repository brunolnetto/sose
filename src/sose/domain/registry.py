from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .entity import Entity

StateChartFactory = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class EntityType:
    name: str
    chart_factory: StateChartFactory


class DomainRegistry:
    def __init__(self) -> None:
        self._types: dict[str, EntityType] = {}

    def register(self, definition: EntityType) -> None:
        if definition.name in self._types:
            raise ValueError(f"entity type already registered: {definition.name}")
        self._types[definition.name] = definition

    def get(self, name: str) -> EntityType:
        return self._types[name]

    def chart_for(self, entity: Entity, **kwargs: Any) -> Any:
        return self.get(entity.entity_type).chart_factory(model=entity, **kwargs)
