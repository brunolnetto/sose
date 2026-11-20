from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sose.core.runtime import (
    PreemptiveResourceDefinition,
    ResourceDefinition,
    StoreDefinition,
)
from sose.persistence.memory import MemoryPersistence

from .entities import Aircraft, CrewAssignment, Flight
from .scenarios import ORIGIN
from .simulation_runtime import (
    flow_correlation_id,
    maintenance_work_order_id,
    part_demand_id,
    build_runtime,
)
from .simulation_maintenance import reconcile_aog_maintenance, complete_aog_maintenance
from .simulation_parts import seed_spare_part, reconcile_part_issue
from .simulation_inspection import ensure_inspection, reconcile_inspection
from .simulation_departures import schedule_departure, reconcile_departure, land_flight

__all__ = [
    "AviationEntities", "ORIGIN", "build_runtime", "complete_aog_maintenance", "ensure_inspection",
    "flow_correlation_id", "land_flight", "maintenance_work_order_id", "part_demand_id",
    "reconcile_aog_maintenance", "reconcile_departure", "reconcile_inspection",
    "reconcile_part_issue", "schedule_departure", "seed_reference", "seed_spare_part",
]


@dataclass(frozen=True, slots=True)
class AviationEntities:
    aircraft_id: str
    leg1_id: str
    leg2_id: str
    leg1_crew_id: str
    leg2_crew_id: str


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tail_number: str = "N-SOSE",
    leg1_number: str = "SOSE101",
    leg2_number: str = "SOSE102",
    flight_crew_capacity: int = 1,
    inspection_team_capacity: int = 1,
    maintenance_bay_capacity: int = 1,
    maintenance_queue_capacity: int = 100,
    part_lot_capacity: int = 100,
) -> AviationEntities:
    context, engine = build_runtime(persistence, owner_id=owner_id, now=now)
    aircraft = context.entities.create(
        Aircraft,
        key=("aviation-reference", "aircraft-1"),
        state="available",
        attributes={"tail_number": tail_number, "rotation": f"{leg1_number}/{leg2_number}"},
    )
    leg1 = context.entities.create(
        Flight,
        key=("aviation-reference", "flight-101"),
        state="scheduled",
        attributes={
            "flight_number": leg1_number,
            "aircraft_id": aircraft.id,
            "sequence": 1,
        },
    )
    leg2 = context.entities.create(
        Flight,
        key=("aviation-reference", "flight-102"),
        state="scheduled",
        attributes={
            "flight_number": leg2_number,
            "aircraft_id": aircraft.id,
            "sequence": 2,
            "predecessor_flight_id": leg1.id,
        },
    )
    crew1 = context.entities.create(
        CrewAssignment,
        key=("aviation-reference", leg1.id, "crew"),
        state="planned",
        attributes={"flight_id": leg1.id},
    )
    crew2 = context.entities.create(
        CrewAssignment,
        key=("aviation-reference", leg2.id, "crew"),
        state="planned",
        attributes={"flight_id": leg2.id},
    )
    with persistence.transaction() as uow:
        for entity in (aircraft, leg1, leg2, crew1, crew2):
            uow.save_entity(entity)
        uow.save_resource_definition(ResourceDefinition("flight_crew", capacity=flight_crew_capacity))
        uow.save_resource_definition(ResourceDefinition("inspection_team", capacity=inspection_team_capacity))
    engine.preemptive_resources.define(
        PreemptiveResourceDefinition("maintenance_bay", capacity=maintenance_bay_capacity)
    )
    engine.stores.define(
        StoreDefinition("maintenance_queue", kind="priority", capacity=maintenance_queue_capacity)
    )
    engine.stores.define(
        StoreDefinition("part_lots", kind="priority", capacity=part_lot_capacity)
    )
    return AviationEntities(
        aircraft_id=aircraft.id,
        leg1_id=leg1.id,
        leg2_id=leg2.id,
        leg1_crew_id=crew1.id,
        leg2_crew_id=crew2.id,
    )
