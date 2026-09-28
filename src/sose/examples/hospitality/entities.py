from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Hotel(Entity):
    entity_type: str = "hospitality_hotel"


@dataclass(slots=True)
class Room(Entity):
    entity_type: str = "hospitality_room"


@dataclass(slots=True)
class Reservation(Entity):
    entity_type: str = "hospitality_reservation"


@dataclass(slots=True)
class RoomBooking(Entity):
    entity_type: str = "hospitality_room_booking"


@dataclass(slots=True)
class NoShowOccurrence(Entity):
    entity_type: str = "hospitality_no_show_occurrence"
