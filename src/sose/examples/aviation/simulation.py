from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: F401

from sose.backends.simpy import SimPyBackend  # noqa: F401
from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine  # noqa: F401
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import (
    PreemptiveResourceDefinition,
    ResourceDefinition,
    StoreDefinition,
)
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import (
    Aircraft,
    CrewAssignment,
    Flight,
    Inspection,  # noqa: F401
    MaintenanceWorkOrder,  # noqa: F401
    PartDemand,  # noqa: F401
)
from .scenarios import ORIGIN
from .statecharts import (
    AircraftChart,  # noqa: F401
    CrewAssignmentChart,  # noqa: F401
    FlightChart,  # noqa: F401
    InspectionChart,  # noqa: F401
    MaintenanceWorkOrderChart,  # noqa: F401
    PartDemandChart,  # noqa: F401
)
from .simulation_runtime import (  # noqa: F401 - re-exported, historical import location
    flow_correlation_id,
    maintenance_work_order_id, part_demand_id, build_runtime,
)
from .simulation_maintenance import (  # noqa: F401 - re-exported, historical import location
    reconcile_aog_maintenance,
    complete_aog_maintenance,
)
from .simulation_parts import (  # noqa: F401 - re-exported, historical import location
    seed_spare_part, reconcile_part_issue,
)
from .simulation_inspection import (  # noqa: F401 - re-exported, historical import location
    ensure_inspection, reconcile_inspection,
)
from .simulation_departures import (  # noqa: F401 - re-exported, historical import location
    schedule_departure, reconcile_departure, land_flight,
)


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


# Moved by the quality-ratchet splits (#632); importing these names from here
# is deprecated (ADR-0011). Import them from the module named on the right.
from sose._deprecation import deprecated_reexports as _deprecated_reexports

__getattr__ = _deprecated_reexports(__name__, {
    "deterministic_id": "sose.core.identity",
    "ensure_maintenance": "sose.examples.aviation.simulation_maintenance",
    "inspection_id": "sose.examples.aviation.simulation_runtime",
    "preemption_result_for": "sose.examples.aviation.simulation_maintenance",
    "schedule_rotation": "sose.examples.aviation.simulation_departures",
}, since="0.8.1", removed_in="0.9.0")
