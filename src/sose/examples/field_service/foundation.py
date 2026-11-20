"""Field-service constants, ids, runtime/seed setup and lookups (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import CounterRandomSource
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import Appointment, Technician, WorkOrder
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
PART_FILTER_KEY = f"sku:{REQUIRED_PART}"


def _required_part_filter(item) -> bool:
    return item.value.get("sku") == REQUIRED_PART


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
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 1223,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=CounterRandomSource(root_seed=random_seed),
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
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
        store_filters={PART_FILTER_KEY: _required_part_filter},
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    territory: str = REQUIRED_TERRITORY,
    required_skill: str = REQUIRED_SKILL,
    wrong_skill: str = "copper-installation",
    technician_resource_capacity: int = 1,
    parts_store_capacity: int = 20,
) -> FieldServiceEntities:
    context, engine = build_runtime(persistence, owner_id=owner_id, now=now)
    work_order = context.entities.create(
        WorkOrder,
        key=("field-service-reference", "installation-1"),
        state="ready",
        attributes={
            "customer_id": "customer-1",
            "place_id": "premises-1",
            "territory": territory,
            "required_skills": [required_skill],
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
            "skills": [wrong_skill],
            "territories": [territory],
            "bookings": [],
        },
    )
    qualified = context.entities.create(
        Technician,
        key=("field-service-reference", "technician-2"),
        state="available",
        attributes={
            "technician_key": "technician-2",
            "skills": [required_skill, "wifi-commissioning"],
            "territories": [territory, "central"],
            "bookings": [],
        },
    )
    with persistence.transaction() as uow:
        for entity in (work_order, wrong_skill, qualified):
            uow.save_entity(entity)
        for technician in (wrong_skill, qualified):
            uow.save_resource_definition(
                ResourceDefinition(
                    f"field-tech:{technician.id}",
                    capacity=technician_resource_capacity,
                )
            )
    engine.stores.define(StoreDefinition("field_parts", kind="filter", capacity=parts_store_capacity))
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
