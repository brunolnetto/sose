"""Hospitality constants, ids, runtime/seed setup and lookups (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import Hotel, Reservation, Room, RoomBooking
from .scenarios import ORIGIN
from .statecharts import NoShowOccurrenceChart, ReservationChart, RoomBookingChart


DEFAULT_HOLD = timedelta(hours=1)
DEFAULT_NO_SHOW_GRACE = timedelta(hours=2)
BLOCKING_BOOKING_STATES = frozenset({"held", "confirmed", "occupied"})


@dataclass(frozen=True, slots=True)
class HospitalityEntities:
    hotel_id: str
    room_ids: tuple[str, ...]


def flow_correlation_id(reservation_id: str) -> str:
    return deterministic_id("hospitality-flow", reservation_id)


def reservation_id(ordinal: int) -> str:
    return deterministic_id(
        "entity",
        "hospitality_reservation",
        "hospitality-reference",
        "reservation",
        ordinal,
    )


def booking_id(reservation_id_value: str) -> str:
    return deterministic_id(
        "entity",
        "hospitality_room_booking",
        "hospitality-reference",
        reservation_id_value,
        "room-booking",
    )


def no_show_id(reservation_id_value: str) -> str:
    return deterministic_id(
        "entity",
        "hospitality_no_show_occurrence",
        "hospitality-reference",
        reservation_id_value,
        "no-show",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 1319,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("hospitality_reservation", ReservationChart))
    registry.register(EntityType("hospitality_room_booking", RoomBookingChart))
    registry.register(
        EntityType("hospitality_no_show_occurrence", NoShowOccurrenceChart)
    )
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    room_count: int = 2,
    room_type: str = "standard",
) -> HospitalityEntities:
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
    rooms = tuple(
        context.entities.create(
            Room,
            key=("hospitality-reference", f"room-{101 + index}"),
            state="inventory",
            attributes={
                "room_number": str(101 + index),
                "room_type": room_type,
            },
        )
        for index in range(room_count)
    )
    hotel = context.entities.create(
        Hotel,
        key=("hospitality-reference", "hotel-1"),
        state="open",
        attributes={
            "room_ids": [room.id for room in rooms],
            "booking_ids": [],
        },
    )
    with persistence.transaction() as uow:
        for entity in (*rooms, hotel):
            uow.save_entity(entity)
    return HospitalityEntities(
        hotel_id=hotel.id,
        room_ids=tuple(room.id for room in rooms),
    )


def _entity(persistence, entity_type: str, entity_id: str):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _hotel(persistence: MemoryPersistence, entities: HospitalityEntities) -> Hotel:
    return _entity(persistence, "hospitality_hotel", entities.hotel_id)


def _reservation_entity(
    persistence: MemoryPersistence,
    reservation_id_value: str,
) -> Reservation:
    return _entity(
        persistence,
        "hospitality_reservation",
        reservation_id_value,
    )


def _booking(
    persistence: MemoryPersistence,
    reservation_id_value: str,
) -> RoomBooking:
    return _entity(
        persistence,
        "hospitality_room_booking",
        booking_id(reservation_id_value),
    )


def _dispatch(engine: Engine, entity, event: str, *, key: tuple[object, ...]) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=flow_correlation_id(
            str(
                entity.attributes.get("reservation_id")
                or entity.id
            )
        ),
        key=key,
    )
    engine.dispatch(command)


def _at(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _overlaps(
    left_start: datetime,
    left_end: datetime,
    right_start: datetime,
    right_end: datetime,
) -> bool:
    return left_start < right_end and right_start < left_end
