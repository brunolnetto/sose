from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class FlightTurnaround(Entity):
    entity_type: str = "airport_flight_turnaround"


@dataclass(slots=True)
class GateAssignment(Entity):
    entity_type: str = "airport_gate_assignment"


@dataclass(slots=True)
class GroundServiceTask(Entity):
    entity_type: str = "airport_ground_service_task"


@dataclass(slots=True)
class BaggageFlow(Entity):
    entity_type: str = "airport_baggage_flow"


@dataclass(slots=True)
class DepartureSlot(Entity):
    entity_type: str = "airport_departure_slot"
