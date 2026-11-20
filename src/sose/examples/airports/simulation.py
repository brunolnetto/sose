from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.persistence.memory import MemoryPersistence

from .entities import (
    BaggageFlow,
    DepartureSlot,
    FlightTurnaround,
    GateAssignment,
    GroundServiceTask,
)
from .scenarios import ORIGIN
from .simulation_runtime import flow_correlation_id, build_runtime
from .simulation_gates import schedule_arrival, reconcile_gate
from .simulation_ground import reconcile_ground_service, reconcile_baggage
from .simulation_departures import schedule_departure_slot, queue_departure, reconcile_departure

__all__ = [
    "AirportEntities", "ORIGIN", "build_runtime", "flow_correlation_id", "queue_departure",
    "reconcile_baggage", "reconcile_departure", "reconcile_gate", "reconcile_ground_service",
    "schedule_arrival", "schedule_departure_slot", "seed_reference",
]


@dataclass(frozen=True, slots=True)
class AirportEntities:
    turnaround_id: str
    gate_assignment_id: str
    service_task_id: str
    baggage_flow_id: str
    departure_slot_id: str


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    flight_number: str = "SOSE101",
    departure_priority: int = 50,
    gate: str = "G1",
    gate_capacity: int = 1,
    ground_team_capacity: int = 1,
    tug_capacity: int = 1,
    departure_queue_capacity: int = 100,
) -> AirportEntities:
    context, engine = build_runtime(persistence, owner_id=owner_id, now=now)
    turnaround = context.entities.create(
        FlightTurnaround,
        key=("airport-reference", flight_number, "turnaround"),
        state="scheduled",
        attributes={
            "flight_number": flight_number,
            "departure_priority": int(departure_priority),
        },
    )
    gate_assignment = context.entities.create(
        GateAssignment,
        key=("airport-reference", turnaround.id, "gate"),
        state="planned",
        attributes={"turnaround_id": turnaround.id, "gate": gate},
    )
    service_task = context.entities.create(
        GroundServiceTask,
        key=("airport-reference", turnaround.id, "service"),
        state="pending",
        attributes={"turnaround_id": turnaround.id, "service": "turnaround-service"},
    )
    baggage_flow = context.entities.create(
        BaggageFlow,
        key=("airport-reference", turnaround.id, "baggage"),
        state="pending",
        attributes={"turnaround_id": turnaround.id},
    )
    departure_slot = context.entities.create(
        DepartureSlot,
        key=("airport-reference", turnaround.id, "departure-slot"),
        state="planned",
        attributes={"turnaround_id": turnaround.id, "priority": int(departure_priority)},
    )
    with persistence.transaction() as uow:
        for entity in (
            turnaround,
            gate_assignment,
            service_task,
            baggage_flow,
            departure_slot,
        ):
            uow.save_entity(entity)
        uow.save_resource_definition(ResourceDefinition("gate", capacity=gate_capacity))
        uow.save_resource_definition(ResourceDefinition("ground_team", capacity=ground_team_capacity))
        uow.save_resource_definition(ResourceDefinition("tug", capacity=tug_capacity))
    engine.stores.define(
        StoreDefinition("departure_queue", kind="priority", capacity=departure_queue_capacity)
    )
    return AirportEntities(
        turnaround_id=turnaround.id,
        gate_assignment_id=gate_assignment.id,
        service_task_id=service_task.id,
        baggage_flow_id=baggage_flow.id,
        departure_slot_id=departure_slot.id,
    )
