from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class ServicePoint(Entity):
    entity_type: str = "utility_service_point"


@dataclass(slots=True)
class Meter(Entity):
    entity_type: str = "utility_meter"


@dataclass(slots=True)
class MeterReading(Entity):
    entity_type: str = "utility_meter_reading"


@dataclass(slots=True)
class Outage(Entity):
    entity_type: str = "utility_outage"


@dataclass(slots=True)
class DemandResponseEvent(Entity):
    entity_type: str = "utility_dr_event"


@dataclass(slots=True)
class DemandResponseParticipation(Entity):
    entity_type: str = "utility_dr_participation"
