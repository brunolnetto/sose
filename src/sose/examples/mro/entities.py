from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class WorkOrder(Entity):
    entity_type: str = "work_order"
