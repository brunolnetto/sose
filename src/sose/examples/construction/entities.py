from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class ConstructionActivity(Entity):
    entity_type: str = "construction_activity"


@dataclass(slots=True)
class ConstructionInspection(Entity):
    entity_type: str = "construction_inspection"
