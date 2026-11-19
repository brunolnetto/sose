"""Field-service technician selection and appointment scheduling (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import Appointment, Technician, WorkOrder
from .foundation import (
    FieldServiceEntities,
    _appointment,
    _at,
    _dispatch,
    _overlaps,
    _technician,
    _work_order,
    appointment_id,
    flow_correlation_id,
)


def _technician_is_eligible(
    technician: Technician,
    *,
    required_skills: tuple[str, ...],
    territory: str,
    start_at: datetime,
    end_at: datetime,
) -> bool:
    if not set(required_skills).issubset(set(technician.attributes.get("skills", []))):
        return False
    if territory not in technician.attributes.get("territories", []):
        return False
    for booking in technician.attributes.get("bookings", []):
        if _overlaps(
            start_at,
            end_at,
            _at(booking["start_at"]),
            _at(booking["end_at"]),
        ):
            return False
    return True


def select_technician(
    persistence: MemoryPersistence,
    *,
    entities: FieldServiceEntities,
    start_at: datetime,
    end_at: datetime,
) -> Technician | None:
    work_order = _work_order(persistence, entities)
    required_skills = tuple(str(v) for v in work_order.attributes["required_skills"])
    territory = str(work_order.attributes["territory"])
    eligible = [
        _technician(persistence, technician_id)
        for technician_id in entities.technician_ids
        if _technician_is_eligible(
            _technician(persistence, technician_id),
            required_skills=required_skills,
            territory=territory,
            start_at=start_at,
            end_at=end_at,
        )
    ]
    return None if not eligible else sorted(eligible, key=lambda value: value.id)[0]


def propose_appointment(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: FieldServiceEntities,
    ordinal: int,
    start_at: datetime,
    end_at: datetime,
    replaces_appointment_id: str | None = None,
) -> Appointment:
    if ordinal <= 0:
        raise ValueError("appointment ordinal must be positive")
    if end_at <= start_at:
        raise ValueError("appointment end must be after start")
    work_order = _work_order(persistence, entities)
    if work_order.state not in {"ready", "reschedule_required"}:
        raise RuntimeError(
            f"appointment requires schedulable work order, got {work_order.state}"
        )
    active_id = work_order.attributes.get("active_appointment_id")
    if active_id is not None:
        active = persistence.entity("field_appointment", str(active_id))
        if active is not None and active.state not in {
            "completed",
            "no_access_recorded",
            "missed",
            "cancelled",
        }:
            raise RuntimeError("work order already has an active appointment")
        work_order.attributes["active_appointment_id"] = None
    aid = appointment_id(work_order.id, ordinal)
    existing = persistence.entity("field_appointment", aid)
    if existing is not None:
        expected = (
            start_at.isoformat(),
            end_at.isoformat(),
            replaces_appointment_id,
        )
        actual = (
            existing.attributes["start_at"],
            existing.attributes["end_at"],
            existing.attributes.get("replaces_appointment_id"),
        )
        if actual != expected:
            raise ValueError("appointment identity already exists with different window")
        return existing

    appointment = engine.context.entities.create(
        Appointment,
        key=("field-service-reference", work_order.id, "appointment", ordinal),
        state="proposed",
        attributes={
            "work_order_id": work_order.id,
            "ordinal": ordinal,
            "place_id": work_order.attributes["place_id"],
            "territory": work_order.attributes["territory"],
            "required_skills": list(work_order.attributes["required_skills"]),
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "technician_id": None,
            "replaces_appointment_id": replaces_appointment_id,
        },
    )
    appointment_ids = list(work_order.attributes.get("appointment_ids", []))
    if appointment.id not in appointment_ids:
        appointment_ids.append(appointment.id)
        work_order.attributes["appointment_ids"] = appointment_ids
    with persistence.transaction() as uow:
        uow.save_entity(appointment)
        uow.save_entity(work_order)
    return appointment


def _ensure_appointment_boundaries(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    appointment: Appointment,
    work_order: WorkOrder,
) -> None:
    start_at = _at(appointment.attributes["start_at"])
    end_at = _at(appointment.attributes["end_at"])
    correlation_id = flow_correlation_id(str(appointment.attributes["work_order_id"]))
    if appointment.state == "confirmed" and engine.scheduler.find_pending(
        entity_type="field_appointment",
        entity_id=appointment.id,
        name="start",
    ) is None:
        command = engine.context.commands.create(
            "start",
            target=appointment,
            due_at=start_at,
            correlation_id=correlation_id,
            key=("field-appointment", appointment.id, "start"),
        )
        engine.context.schedules.at(start_at, command=command)
    if appointment.state in {"confirmed", "in_progress"} and engine.scheduler.find_pending(
        entity_type="field_appointment",
        entity_id=appointment.id,
        name="miss",
    ) is None:
        command = engine.context.commands.create(
            "miss",
            target=appointment,
            due_at=end_at,
            correlation_id=correlation_id,
            key=("field-appointment", appointment.id, "miss"),
        )
        engine.context.schedules.at(end_at, command=command)
    if work_order.state in {"scheduled", "in_progress"} and engine.scheduler.find_pending(
        entity_type="field_work_order",
        entity_id=work_order.id,
        name="require_reschedule",
    ) is None:
        command = engine.context.commands.create(
            "require_reschedule",
            target=work_order,
            due_at=end_at,
            correlation_id=correlation_id,
            key=("field-work-order", work_order.id, appointment.id, "window-expired"),
        )
        engine.context.schedules.at(end_at, command=command)


def confirm_appointment(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: FieldServiceEntities,
    appointment_id_value: str,
) -> Appointment:
    appointment = _appointment(persistence, appointment_id_value)
    work_order = _work_order(persistence, entities)
    active_id = work_order.attributes.get("active_appointment_id")
    if active_id is not None and active_id != appointment.id:
        active = persistence.entity("field_appointment", str(active_id))
        if active is not None and active.state not in {
            "completed",
            "no_access_recorded",
            "missed",
            "cancelled",
        }:
            raise RuntimeError("work order already has an active appointment")
    start_at = _at(appointment.attributes["start_at"])
    end_at = _at(appointment.attributes["end_at"])

    if appointment.attributes.get("technician_id") is None:
        technician = select_technician(
            persistence,
            entities=entities,
            start_at=start_at,
            end_at=end_at,
        )
        if technician is None:
            raise RuntimeError("no skill/territory/time eligible technician")
        bookings = list(technician.attributes.get("bookings", []))
        bookings.append(
            {
                "appointment_id": appointment.id,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
            }
        )
        technician.attributes["bookings"] = bookings
        appointment.attributes["technician_id"] = technician.id
        work_order.attributes["active_appointment_id"] = appointment.id
        with persistence.transaction() as uow:
            uow.save_entity(technician)
            uow.save_entity(appointment)
            uow.save_entity(work_order)

    appointment = _appointment(persistence, appointment.id)
    correlation_id = flow_correlation_id(work_order.id)
    if appointment.state == "proposed":
        _dispatch(
            engine,
            appointment,
            "confirm",
            key=("field-appointment", appointment.id, "confirm"),
            correlation_id=correlation_id,
        )
        appointment = _appointment(persistence, appointment.id)

    work_order = _work_order(persistence, entities)
    if work_order.state == "ready":
        _dispatch(
            engine,
            work_order,
            "schedule",
            key=("field-work-order", work_order.id, appointment.id, "schedule"),
            correlation_id=correlation_id,
        )
    elif work_order.state == "reschedule_required":
        _dispatch(
            engine,
            work_order,
            "reschedule",
            key=("field-work-order", work_order.id, appointment.id, "reschedule"),
            correlation_id=correlation_id,
        )

    work_order = _work_order(persistence, entities)
    _ensure_appointment_boundaries(
        persistence,
        engine,
        appointment=appointment,
        work_order=work_order,
    )
    return _appointment(persistence, appointment.id)
