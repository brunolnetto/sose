from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Vehicle(Entity):
    entity_type: str = "transit_vehicle"


@dataclass(slots=True)
class ScheduledTrip(Entity):
    entity_type: str = "transit_scheduled_trip"


@dataclass(slots=True)
class TripUpdateOccurrence(Entity):
    entity_type: str = "transit_trip_update"


@dataclass(slots=True)
class VehiclePositionOccurrence(Entity):
    entity_type: str = "transit_vehicle_position"


@dataclass(slots=True)
class ServiceAlert(Entity):
    entity_type: str = "transit_service_alert"
