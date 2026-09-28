from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class WorkOrder(Entity):
    entity_type: str = "field_work_order"


@dataclass(slots=True)
class Technician(Entity):
    entity_type: str = "field_technician"


@dataclass(slots=True)
class Appointment(Entity):
    entity_type: str = "field_appointment"


@dataclass(slots=True)
class VisitOccurrence(Entity):
    entity_type: str = "field_visit_occurrence"
