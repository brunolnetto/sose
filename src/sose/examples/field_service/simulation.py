from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import Appointment, Technician, VisitOccurrence, WorkOrder
from .scenarios import ORIGIN
from .statecharts import (
    AppointmentChart,
    TechnicianChart,
    VisitOccurrenceChart,
    WorkOrderChart,
)


REQUIRED_SKILL = "fiber-installation"
REQUIRED_TERRITORY = "west"
REQUIRED_PART = "ont-router"


@dataclass(frozen=True, slots=True)
class FieldServiceEntities:
    work_order_id: str
    technician_ids: tuple[str, ...]


def flow_correlation_id(work_order_id: str) -> str:
    return deterministic_id("field-service-flow", work_order_id)


def appointment_id(work_order_id: str, ordinal: int) -> str:
    return deterministic_id(
        "entity",
        "field_appointment",
        "field-service-reference",
        work_order_id,
        "appointment",
        ordinal,
    )


def visit_id(appointment_id_value: str, sequence: int) -> str:
    return deterministic_id(
        "entity",
        "field_visit_occurrence",
        "field-service-reference",
        appointment_id_value,
        "visit",
        sequence,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=1223),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("field_work_order", WorkOrderChart))
    registry.register(EntityType("field_technician", TechnicianChart))
    registry.register(EntityType("field_appointment", AppointmentChart))
    registry.register(EntityType("field_visit_occurrence", VisitOccurrenceChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(persistence: MemoryPersistence) -> FieldServiceEntities:
    context, engine = build_runtime(persistence)
    work_order = context.entities.create(
        WorkOrder,
        key=("field-service-reference", "installation-1"),
        state="ready",
        attributes={
            "customer_id": "customer-1",
            "place_id": "premises-1",
            "territory": REQUIRED_TERRITORY,
            "required_skills": [REQUIRED_SKILL],
            "required_part": REQUIRED_PART,
            "active_appointment_id": None,
            "appointment_ids": [],
        },
    )
    wrong_skill = context.entities.create(
        Technician,
        key=("field-service-reference", "technician-1"),
        state="available",
        attributes={
            "technician_key": "technician-1",
            "skills": ["copper-installation"],
            "territories": [REQUIRED_TERRITORY],
            "bookings": [],
        },
    )
    qualified = context.entities.create(
        Technician,
        key=("field-service-reference", "technician-2"),
        state="available",
        attributes={
            "technician_key": "technician-2",
            "skills": [REQUIRED_SKILL, "wifi-commissioning"],
            "territories": [REQUIRED_TERRITORY, "central"],
            "bookings": [],
        },
    )
    with persistence.transaction() as uow:
        for entity in (work_order, wrong_skill, qualified):
            uow.save_entity(entity)
        for technician in (wrong_skill, qualified):
            uow.save_resource_definition(
                ResourceDefinition(f"field-tech:{technician.id}", capacity=1)
            )
    engine.stores.define(StoreDefinition("field_parts", kind="fifo", capacity=20))
    return FieldServiceEntities(
        work_order_id=work_order.id,
        technician_ids=(wrong_skill.id, qualified.id),
    )


def seed_part_inventory(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
) -> None:
    existing = any(
        item.item_id == "ont-router-1"
        for item in persistence.store_items()
    )
    pending = any(
        intent.item_id == "ont-router-1"
        for intent in persistence.store_put_intents()
    )
    if not existing and not pending:
        engine.stores.put(
            backend,
            store_name="field_parts",
            item_id="ont-router-1",
            value={"sku": REQUIRED_PART, "serial": "ONT-0001"},
            requested_at=backend.now,
        )
        backend.run_until(backend.now)


def _entity(persistence, entity_type: str, entity_id: str):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _work_order(
    persistence: MemoryPersistence,
    entities: FieldServiceEntities,
) -> WorkOrder:
    return _entity(persistence, "field_work_order", entities.work_order_id)


def _appointment(
    persistence: MemoryPersistence,
    appointment_id_value: str,
) -> Appointment:
    return _entity(persistence, "field_appointment", appointment_id_value)


def _technician(
    persistence: MemoryPersistence,
    technician_id: str,
) -> Technician:
    return _entity(persistence, "field_technician", technician_id)


def _dispatch(engine, entity, event: str, *, key, correlation_id: str) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
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
    if work_order.state not in {"ready", "reschedule_required", "scheduled"}:
        raise RuntimeError(
            f"appointment requires schedulable work order, got {work_order.state}"
        )
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
    engine: Engine,
    appointment: Appointment,
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


def confirm_appointment(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: FieldServiceEntities,
    appointment_id_value: str,
) -> Appointment:
    appointment = _appointment(persistence, appointment_id_value)
    work_order = _work_order(persistence, entities)
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

    _ensure_appointment_boundaries(engine, appointment)
    return _appointment(persistence, appointment.id)


def reserve_required_part(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: FieldServiceEntities,
) -> bool:
    work_order = _work_order(persistence, entities)
    request_id = f"field-part:{work_order.id}"
    existing = next(
        (
            result
            for result in persistence.store_get_results()
            if result.request_id == request_id
        ),
        None,
    )
    if existing is not None:
        if existing.item.value.get("sku") != work_order.attributes["required_part"]:
            raise RuntimeError("reserved field part does not satisfy work order")
        return True

    result = engine.stores.ensure_selection(
        backend,
        store_name="field_parts",
        request_id=request_id,
        requested_at=backend.now,
    )
    if result is None:
        return False
    if result.item.value.get("sku") != work_order.attributes["required_part"]:
        raise RuntimeError("selected field part does not satisfy work order")
    return True


def reconcile_work_start(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: FieldServiceEntities,
    appointment_id_value: str,
) -> bool:
    appointment = _appointment(persistence, appointment_id_value)
    work_order = _work_order(persistence, entities)
    if appointment.state != "in_progress":
        return False
    if not engine.context.scenarios.attribute("field_service.dispatch.available", True):
        return False
    if not reserve_required_part(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        return False

    technician_id = appointment.attributes.get("technician_id")
    if technician_id is None:
        raise RuntimeError("appointment has no technician")
    technician = _technician(persistence, str(technician_id))
    request_id = f"field-tech:{appointment.id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name=f"field-tech:{technician.id}",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(work_order.id)
    technician = _technician(persistence, technician.id)
    if technician.state == "available":
        _dispatch(
            engine,
            technician,
            "assign",
            key=("field-technician", technician.id, appointment.id, "assign"),
            correlation_id=correlation_id,
        )
    work_order = _work_order(persistence, entities)
    if work_order.state == "scheduled":
        _dispatch(
            engine,
            work_order,
            "start",
            key=("field-work-order", work_order.id, appointment.id, "start"),
            correlation_id=correlation_id,
        )
    return True


def _release_technician(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    appointment: Appointment,
    work_order_id: str,
) -> None:
    technician_id = appointment.attributes.get("technician_id")
    if technician_id is None:
        return
    engine.resources.withdraw(backend, f"field-tech:{appointment.id}")
    technician = _technician(persistence, str(technician_id))
    if technician.state == "assigned":
        _dispatch(
            engine,
            technician,
            "release",
            key=("field-technician", technician.id, appointment.id, "release"),
            correlation_id=flow_correlation_id(work_order_id),
        )


def record_visit(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: FieldServiceEntities,
    appointment_id_value: str,
    sequence: int,
    outcome: str,
) -> VisitOccurrence:
    if sequence <= 0:
        raise ValueError("visit sequence must be positive")
    if outcome not in {"completed", "no_access"}:
        raise ValueError(f"unsupported visit outcome: {outcome}")

    appointment = _appointment(persistence, appointment_id_value)
    work_order = _work_order(persistence, entities)
    vid = visit_id(appointment.id, sequence)
    existing = persistence.entity("field_visit_occurrence", vid)
    if existing is not None:
        if existing.attributes["outcome"] != outcome:
            raise ValueError("visit identity already exists with different outcome")
        return existing
    if appointment.state != "in_progress" or work_order.state != "in_progress":
        raise RuntimeError("visit outcome requires active appointment and work order")

    visit = engine.context.entities.create(
        VisitOccurrence,
        key=("field-service-reference", appointment.id, "visit", sequence),
        state="captured",
        attributes={
            "work_order_id": work_order.id,
            "appointment_id": appointment.id,
            "technician_id": appointment.attributes["technician_id"],
            "outcome": outcome,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(visit)
    correlation_id = flow_correlation_id(work_order.id)
    _dispatch(
        engine,
        visit,
        "commit",
        key=("field-visit", visit.id, "commit"),
        correlation_id=correlation_id,
    )

    engine.scheduler.cancel_pending(
        entity_type="field_appointment",
        entity_id=appointment.id,
        name="miss",
    )
    appointment = _appointment(persistence, appointment.id)
    work_order = _work_order(persistence, entities)
    if outcome == "no_access":
        _dispatch(
            engine,
            appointment,
            "no_access",
            key=("field-appointment", appointment.id, "no-access"),
            correlation_id=correlation_id,
        )
        _dispatch(
            engine,
            work_order,
            "require_reschedule",
            key=("field-work-order", work_order.id, appointment.id, "no-access"),
            correlation_id=correlation_id,
        )
    else:
        _dispatch(
            engine,
            appointment,
            "complete",
            key=("field-appointment", appointment.id, "complete"),
            correlation_id=correlation_id,
        )
        _dispatch(
            engine,
            work_order,
            "complete",
            key=("field-work-order", work_order.id, appointment.id, "complete"),
            correlation_id=correlation_id,
        )

    _release_technician(
        persistence,
        engine,
        backend,
        appointment=appointment,
        work_order_id=work_order.id,
    )
    return _entity(persistence, "field_visit_occurrence", visit.id)


def run_happy_path() -> tuple[MemoryPersistence, FieldServiceEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    seed_part_inventory(persistence, engine, backend)

    appointment = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    )
    appointment = confirm_appointment(
        persistence,
        engine,
        entities=entities,
        appointment_id_value=appointment.id,
    )
    backend.run_until(_at(appointment.attributes["start_at"]))
    if not reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    ):
        raise RuntimeError("field work did not start")
    record_visit(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
        sequence=1,
        outcome="completed",
    )
    return persistence, entities
