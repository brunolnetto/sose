from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Shipment(Entity):
    entity_type: str = "shipment"


@dataclass(slots=True)
class DeliveryAttempt(Entity):
    entity_type: str = "delivery_attempt"
