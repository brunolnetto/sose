from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Aircraft(Entity):
    entity_type: str = "aviation_aircraft"


@dataclass(slots=True)
class Flight(Entity):
    entity_type: str = "aviation_flight"


@dataclass(slots=True)
class CrewAssignment(Entity):
    entity_type: str = "aviation_crew_assignment"


@dataclass(slots=True)
class Inspection(Entity):
    entity_type: str = "aviation_inspection"


@dataclass(slots=True)
class MaintenanceWorkOrder(Entity):
    entity_type: str = "aviation_maintenance_work_order"


@dataclass(slots=True)
class PartDemand(Entity):
    entity_type: str = "aviation_part_demand"
