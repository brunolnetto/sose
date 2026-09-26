from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class ProductionOrder(Entity):
    entity_type: str = "production_order"


@dataclass(slots=True)
class Operation(Entity):
    entity_type: str = "manufacturing_operation"
