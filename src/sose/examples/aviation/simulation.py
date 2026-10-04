from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import (
    PreemptiveResourceDefinition,
    ResourceDefinition,
    StoreDefinition,
)
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import (
    Aircraft,
    CrewAssignment,
    Flight,
    Inspection,
    MaintenanceWorkOrder,
    PartDemand,
)
from .scenarios import ORIGIN
from .statecharts import (
    AircraftChart,
    CrewAssignmentChart,
    FlightChart,
    InspectionChart,
    MaintenanceWorkOrderChart,
    PartDemandChart,
)


LEG1_DEPARTURE_DELAY = timedelta(hours=1)
LEG2_DEPARTURE_DELAY = timedelta(hours=5)


@dataclass(frozen=True, slots=True)
class AviationEntities:
    aircraft_id: str
    leg1_id: str
    leg2_id: str
    leg1_crew_id: str
    leg2_crew_id: str


def flow_correlation_id(aircraft_id: str) -> str:
    return deterministic_id("aviation-rotation-flow", aircraft_id)


def inspection_id(flight_id: str) -> str:
    return deterministic_id(
        "entity",
        "aviation_inspection",
        "aviation-reference",
        flight_id,
        "inspection",
    )


def maintenance_work_order_id(flight_id: str) -> str:
    return deterministic_id(
        "entity",
        "aviation_maintenance_work_order",
        "aviation-reference",
        flight_id,
        "maintenance",
    )


def part_demand_id(flight_id: str) -> str:
    return deterministic_id(
        "entity",
        "aviation_part_demand",
        "aviation-reference",
        flight_id,
        "part-demand",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 713,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("aviation_aircraft", AircraftChart))
    registry.register(EntityType("aviation_flight", FlightChart))
    registry.register(EntityType("aviation_crew_assignment", CrewAssignmentChart))
    registry.register(EntityType("aviation_inspection", InspectionChart))
    registry.register(
        EntityType(
            "aviation_maintenance_work_order",
            MaintenanceWorkOrderChart,
        )
    )
    registry.register(EntityType("aviation_part_demand", PartDemandChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
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
    context, engine = build_runtime(persistence, now=now)
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


def _entity(persistence, entity_type, entity_id):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _aircraft(persistence, entities):
    return _entity(persistence, "aviation_aircraft", entities.aircraft_id)


def _flight(persistence, flight_id):
    return _entity(persistence, "aviation_flight", flight_id)


def _crew(persistence, crew_id):
    return _entity(persistence, "aviation_crew_assignment", crew_id)


def _dispatch(engine, entity, event, *, key, correlation_id):
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def schedule_departure(
    persistence,
    engine,
    backend,
    *,
    flight_id,
    delay,
):
    flight = _flight(persistence, flight_id)
    if flight.state != "scheduled":
        return backend.now
    existing = engine.scheduler.find_pending(
        entity_type="aviation_flight",
        entity_id=flight.id,
        name="make_due",
    )
    if existing is not None:
        return existing.work.due_at
    due_at = backend.now + delay
    command = engine.context.commands.create(
        "make_due",
        target=flight,
        due_at=due_at,
        correlation_id=flow_correlation_id(str(flight.attributes["aircraft_id"])),
        key=("aviation", flight.id, "departure-due"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def schedule_rotation(
    persistence,
    engine,
    backend,
    *,
    entities,
):
    first = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=LEG1_DEPARTURE_DELAY,
    )
    second = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg2_id,
        delay=LEG2_DEPARTURE_DELAY,
    )
    return first, second


def _crew_for_flight(entities, flight_id):
    if flight_id == entities.leg1_id:
        return entities.leg1_crew_id
    if flight_id == entities.leg2_id:
        return entities.leg2_crew_id
    raise KeyError(f"unknown reference flight: {flight_id}")


def reconcile_departure(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    crew = _crew(persistence, _crew_for_flight(entities, flight_id))
    correlation_id = flow_correlation_id(aircraft.id)
    request_id = f"flight-crew:{flight.id}"

    if flight.state == "airborne":
        return _reconcile_departure_airborne(
            persistence,
            engine,
            entities=entities,
            flight_id=flight.id,
            aircraft=aircraft,
            correlation_id=correlation_id,
        )
    if flight.state not in {"due", "delayed", "ready"}:
        return False

    if not _departure_prerequisites_ready(
        persistence,
        engine,
        aircraft=aircraft,
        flight=flight,
    ):
        _handle_departure_unavailable(
            engine,
            backend,
            flight=flight,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return False

    if flight.state == "delayed":
        _dispatch(
            engine,
            flight,
            "resume",
            key=("aviation", flight.id, "resume"),
            correlation_id=correlation_id,
        )
        flight = _flight(persistence, flight.id)

    if not _ensure_crew_reservation(
        persistence,
        engine,
        backend,
        crew=crew,
        request_id=request_id,
        correlation_id=correlation_id,
    ):
        return False

    _progress_departure_dispatch(
        persistence,
        engine,
        entities=entities,
        flight_id=flight.id,
        correlation_id=correlation_id,
    )
    return _flight(persistence, flight.id).state == "airborne"


def land_flight(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    crew = _crew(persistence, _crew_for_flight(entities, flight_id))
    if flight.state not in {"airborne", "landed", "inspection"}:
        raise RuntimeError(f"flight cannot reconcile landing from {flight.state}")
    if aircraft.state not in {"airborne", "inspection"}:
        raise RuntimeError(f"aircraft cannot reconcile landing from {aircraft.state}")
    correlation_id = flow_correlation_id(aircraft.id)

    if flight.state == "airborne":
        _dispatch(
            engine,
            flight,
            "land",
            key=("aviation", flight.id, "land"),
            correlation_id=correlation_id,
        )
    aircraft = _aircraft(persistence, entities)
    if aircraft.state == "airborne":
        _dispatch(
            engine,
            aircraft,
            "land",
            key=("aviation-aircraft", aircraft.id, flight.id, "land"),
            correlation_id=correlation_id,
        )

    crew = _crew(persistence, crew.id)
    if crew.state == "active":
        _dispatch(
            engine,
            crew,
            "release",
            key=("aviation-crew", crew.id, "release"),
            correlation_id=correlation_id,
        )
    engine.resources.withdraw(backend, f"flight-crew:{flight.id}")

    flight = _flight(persistence, flight.id)
    if flight.state == "landed":
        _dispatch(
            engine,
            flight,
            "inspect",
            key=("aviation", flight.id, "inspect"),
            correlation_id=correlation_id,
        )
    return ensure_inspection(
        persistence,
        engine,
        flight_id=flight.id,
    )


def ensure_inspection(persistence, engine, *, flight_id):
    existing = persistence.entity("aviation_inspection", inspection_id(flight_id))
    if existing is not None:
        return existing
    flight = _flight(persistence, flight_id)
    if flight.state != "inspection":
        raise RuntimeError("inspection requires Flight(inspection)")
    value = engine.context.entities.create(
        Inspection,
        key=("aviation-reference", flight.id, "inspection"),
        state="pending",
        attributes={"flight_id": flight.id},
    )
    with persistence.transaction() as uow:
        uow.save_entity(value)
    return value


def reconcile_inspection(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
    fail=False,
):
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    inspection = ensure_inspection(
        persistence,
        engine,
        flight_id=flight.id,
    )
    request_id = f"inspection-team:{inspection.id}"
    correlation_id = flow_correlation_id(aircraft.id)

    inspection = _progress_inspection_until_terminal(
        persistence,
        engine,
        backend,
        inspection=inspection,
        request_id=request_id,
        fail=fail,
        correlation_id=correlation_id,
    )

    if inspection.state not in {"passed", "failed"}:
        return False

    engine.resources.withdraw(backend, request_id)

    aircraft = _aircraft(persistence, entities)
    flight = _flight(persistence, flight.id)
    if inspection.state == "passed":
        _release_after_passed_inspection(
            persistence,
            engine,
            entities=entities,
            flight_id=flight.id,
            correlation_id=correlation_id,
        )
        return True

    if aircraft.state == "inspection":
        _dispatch(
            engine,
            aircraft,
            "mark_aog",
            key=("aviation-aircraft", aircraft.id, flight.id, "aog"),
            correlation_id=correlation_id,
        )
    ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=flight.id,
    )
    return False

def ensure_maintenance(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    work_id = maintenance_work_order_id(flight_id)
    work = persistence.entity("aviation_maintenance_work_order", work_id)
    demand = persistence.entity("aviation_part_demand", part_demand_id(flight_id))
    aircraft = _aircraft(persistence, entities)
    correlation_id = flow_correlation_id(aircraft.id)

    if work is None:
        work = engine.context.entities.create(
            MaintenanceWorkOrder,
            key=("aviation-reference", flight_id, "maintenance"),
            state="planned",
            attributes={
                "flight_id": flight_id,
                "aircraft_id": aircraft.id,
                "priority": 1,
                "aog": True,
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(work)
    if demand is None:
        demand = engine.context.entities.create(
            PartDemand,
            key=("aviation-reference", flight_id, "part-demand"),
            state="open",
            attributes={
                "flight_id": flight_id,
                "sku": "AOG-PART",
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(demand)

    work = _entity(persistence, "aviation_maintenance_work_order", work.id)
    if work.state == "planned":
        _dispatch(
            engine,
            work,
            "release",
            key=("aviation-maintenance", work.id, "release"),
            correlation_id=correlation_id,
        )
        work = _entity(persistence, "aviation_maintenance_work_order", work.id)

    queue_item_id = f"maintenance:{work.id}"
    if not any(i.item_id == queue_item_id for i in persistence.store_items()) and not any(
        i.item_id == queue_item_id for i in persistence.store_put_intents()
    ) and not any(
        r.item.item_id == queue_item_id for r in persistence.store_get_results()
    ):
        engine.stores.put(
            backend,
            store_name="maintenance_queue",
            item_id=queue_item_id,
            value={"work_order_id": work.id},
            priority=1,
            requested_at=backend.now,
        )
        backend.run_until(backend.now)

    return work, demand


def seed_spare_part(
    persistence,
    engine,
    backend,
    *,
    item_id="aog-part-lot-1",
):
    if not any(i.item_id == item_id for i in persistence.store_items()) and not any(
        i.item_id == item_id for i in persistence.store_put_intents()
    ):
        engine.stores.put(
            backend,
            store_name="part_lots",
            item_id=item_id,
            value={"sku": "AOG-PART"},
            priority=1,
            requested_at=backend.now,
        )
        backend.run_until(backend.now)


def reconcile_part_issue(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    work, demand = ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=flight_id,
    )
    correlation_id = flow_correlation_id(entities.aircraft_id)

    if demand.state == "issued":
        if work.state == "waiting_part":
            _dispatch(
                engine,
                work,
                "part_ready",
                key=("aviation-maintenance", work.id, "part-ready"),
                correlation_id=correlation_id,
            )
        return True

    if work.state == "released":
        _dispatch(
            engine,
            work,
            "wait_part",
            key=("aviation-maintenance", work.id, "wait-part"),
            correlation_id=correlation_id,
        )

    result = _ensure_part_issue_selection(
        persistence,
        engine,
        backend,
        demand_id=demand.id,
    )
    if result is None:
        return False

    demand = _entity(persistence, "aviation_part_demand", demand.id)
    if demand.state == "open":
        _dispatch(
            engine,
            demand,
            "allocate",
            key=("aviation-part", demand.id, "allocate"),
            correlation_id=correlation_id,
        )
        demand = _entity(persistence, "aviation_part_demand", demand.id)
    if demand.state != "allocated":
        raise RuntimeError(
            f"part demand cannot reconcile issue from {demand.state}"
        )
    _dispatch(
        engine,
        demand,
        "issue",
        key=("aviation-part", demand.id, "issue"),
        correlation_id=correlation_id,
    )
    return reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=flight_id,
    )


def reconcile_aog_maintenance(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    work, demand = ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=flight_id,
    )
    if demand.state != "issued":
        return False
    correlation_id = flow_correlation_id(aircraft.id)

    if work.state == "released":
        _dispatch(
            engine,
            work,
            "wait_bay",
            key=("aviation-maintenance", work.id, "wait-bay"),
            correlation_id=correlation_id,
        )
        work = _entity(persistence, "aviation_maintenance_work_order", work.id)

    if not _ensure_maintenance_queue_selection(
        persistence,
        engine,
        backend,
        work_id=work.id,
    ):
        return False

    request_id = f"maintenance-bay:{work.id}"
    if engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="maintenance_bay",
        request_id=request_id,
        requested_at=backend.now,
        priority=1,
        preempt=True,
    ) is None:
        return False

    _start_aog_maintenance_if_ready(
        persistence,
        engine,
        entities=entities,
        work_id=work.id,
        flight_id=flight.id,
        correlation_id=correlation_id,
    )
    return True


def _reconcile_departure_airborne(
    persistence,
    engine,
    *,
    entities,
    flight_id: str,
    aircraft,
    correlation_id: str,
) -> bool:
    if aircraft.state == "assigned":
        _dispatch(
            engine,
            aircraft,
            "dispatch",
            key=("aviation-aircraft", aircraft.id, flight_id, "dispatch"),
            correlation_id=correlation_id,
        )
        aircraft = _aircraft(persistence, entities)
    return aircraft.state == "airborne"


def _departure_prerequisites_ready(
    persistence,
    engine,
    *,
    aircraft,
    flight,
) -> bool:
    predecessor_id = flight.attributes.get("predecessor_flight_id")
    predecessor_ready = True
    if predecessor_id is not None:
        predecessor = _flight(persistence, str(predecessor_id))
        predecessor_ready = predecessor.state == "released"
    departure_available = engine.context.scenarios.attribute(
        "aviation.departure.available",
        True,
    )
    crew_available = engine.context.scenarios.attribute(
        "aviation.crew.available",
        True,
    )
    aircraft_ready = aircraft.state in {"available", "released"}
    return predecessor_ready and departure_available and crew_available and aircraft_ready


def _handle_departure_unavailable(
    engine,
    backend,
    *,
    flight,
    request_id: str,
    correlation_id: str,
) -> None:
    engine.resources.withdraw(backend, request_id)
    if flight.state not in {"due", "ready"}:
        return
    _dispatch(
        engine,
        flight,
        "delay",
        key=("aviation", flight.id, "delay"),
        correlation_id=correlation_id,
    )


def _ensure_crew_reservation(
    persistence,
    engine,
    backend,
    *,
    crew,
    request_id: str,
    correlation_id: str,
) -> bool:
    if crew.state == "planned":
        _dispatch(
            engine,
            crew,
            "reserve",
            key=("aviation-crew", crew.id, "reserve"),
            correlation_id=correlation_id,
        )
        crew = _crew(persistence, crew.id)
    if engine.resources.ensure_requested(
        backend,
        resource_name="flight_crew",
        request_id=request_id,
        requested_at=backend.now,
    ) is None:
        return False
    crew = _crew(persistence, crew.id)
    if crew.state == "reserved":
        _dispatch(
            engine,
            crew,
            "activate",
            key=("aviation-crew", crew.id, "activate"),
            correlation_id=correlation_id,
        )
    return True


def _progress_departure_dispatch(
    persistence,
    engine,
    *,
    entities,
    flight_id: str,
    correlation_id: str,
) -> None:
    flight = _flight(persistence, flight_id)
    if flight.state == "due":
        _dispatch(
            engine,
            flight,
            "mark_ready",
            key=("aviation", flight.id, "ready"),
            correlation_id=correlation_id,
        )
    aircraft = _aircraft(persistence, entities)
    if aircraft.state in {"available", "released"}:
        _dispatch(
            engine,
            aircraft,
            "assign",
            key=("aviation-aircraft", aircraft.id, flight.id, "assign"),
            correlation_id=correlation_id,
        )
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    if flight.state == "ready" and aircraft.state == "assigned":
        _dispatch(
            engine,
            flight,
            "depart",
            key=("aviation", flight.id, "depart"),
            correlation_id=correlation_id,
        )
        _dispatch(
            engine,
            aircraft,
            "dispatch",
            key=("aviation-aircraft", aircraft.id, flight.id, "dispatch"),
            correlation_id=correlation_id,
        )


def _progress_inspection_until_terminal(
    persistence,
    engine,
    backend,
    *,
    inspection,
    request_id: str,
    fail: bool,
    correlation_id: str,
):
    if inspection.state in {"passed", "failed"}:
        return inspection
    if engine.resources.ensure_requested(
        backend,
        resource_name="inspection_team",
        request_id=request_id,
        requested_at=backend.now,
    ) is None:
        return inspection
    inspection = _entity(
        persistence,
        "aviation_inspection",
        inspection.id,
    )
    if inspection.state == "pending":
        _dispatch(
            engine,
            inspection,
            "begin",
            key=("aviation-inspection", inspection.id, "begin"),
            correlation_id=correlation_id,
        )
        inspection = _entity(
            persistence,
            "aviation_inspection",
            inspection.id,
        )
    if inspection.state == "inspecting":
        _dispatch(
            engine,
            inspection,
            "fail_inspection" if fail else "pass_inspection",
            key=(
                "aviation-inspection",
                inspection.id,
                "fail" if fail else "pass",
            ),
            correlation_id=correlation_id,
        )
        inspection = _entity(
            persistence,
            "aviation_inspection",
            inspection.id,
        )
    return inspection


def _release_after_passed_inspection(
    persistence,
    engine,
    *,
    entities,
    flight_id: str,
    correlation_id: str,
) -> None:
    aircraft = _aircraft(persistence, entities)
    if aircraft.state == "inspection":
        _dispatch(
            engine,
            aircraft,
            "release",
            key=("aviation-aircraft", aircraft.id, flight_id, "release"),
            correlation_id=correlation_id,
        )
    flight = _flight(persistence, flight_id)
    if flight.state == "inspection":
        _dispatch(
            engine,
            flight,
            "release",
            key=("aviation", flight.id, "release"),
            correlation_id=correlation_id,
        )


def _ensure_part_issue_selection(
    persistence,
    engine,
    backend,
    *,
    demand_id: str,
):
    result_id = f"part-issue:{demand_id}"
    result = engine.stores.selection(result_id)
    if result is not None:
        return result
    part_items = sorted(
        (i for i in persistence.store_items() if i.store_name == "part_lots"),
        key=lambda item: (item.priority, item.sequence, item.item_id),
    )
    if not part_items:
        return None
    return engine.stores.ensure_selection(
        backend,
        store_name="part_lots",
        request_id=result_id,
        requested_at=backend.now,
    )


def _progress_part_demand_issue(
    persistence,
    engine,
    *,
    demand_id: str,
    correlation_id: str,
) -> None:
    demand = _entity(persistence, "aviation_part_demand", demand_id)
    if demand.state == "open":
        _dispatch(
            engine,
            demand,
            "allocate",
            key=("aviation-part", demand.id, "allocate"),
            correlation_id=correlation_id,
        )
        demand = _entity(persistence, "aviation_part_demand", demand.id)
    if demand.state == "allocated":
        _dispatch(
            engine,
            demand,
            "issue",
            key=("aviation-part", demand.id, "issue"),
            correlation_id=correlation_id,
        )


def _ensure_maintenance_queue_selection(
    persistence,
    engine,
    backend,
    *,
    work_id: str,
) -> bool:
    queue_request = f"maintenance-pick:{work_id}"
    queue_result = engine.stores.selection(queue_request)
    if queue_result is not None:
        return True
    queue_items = sorted(
        (
            item
            for item in persistence.store_items()
            if item.store_name == "maintenance_queue"
        ),
        key=lambda item: (item.priority, item.sequence, item.item_id),
    )
    if not queue_items or str(queue_items[0].value["work_order_id"]) != work_id:
        return False
    return (
        engine.stores.ensure_selection(
            backend,
            store_name="maintenance_queue",
            request_id=queue_request,
            requested_at=backend.now,
        )
        is not None
    )


def _start_aog_maintenance_if_ready(
    persistence,
    engine,
    *,
    entities,
    work_id: str,
    flight_id: str,
    correlation_id: str,
) -> None:
    work = _entity(persistence, "aviation_maintenance_work_order", work_id)
    if work.state == "waiting_bay":
        _dispatch(
            engine,
            work,
            "start",
            key=("aviation-maintenance", work.id, "start"),
            correlation_id=correlation_id,
        )
    aircraft = _aircraft(persistence, entities)
    if aircraft.state != "aog":
        return
    _dispatch(
        engine,
        aircraft,
        "start_maintenance",
        key=("aviation-aircraft", aircraft.id, flight_id, "maintenance"),
        correlation_id=correlation_id,
    )


def complete_aog_maintenance(
    persistence,
    engine,
    backend,
    *,
    entities,
    flight_id,
):
    work = _entity(
        persistence,
        "aviation_maintenance_work_order",
        maintenance_work_order_id(flight_id),
    )
    flight = _flight(persistence, flight_id)
    aircraft = _aircraft(persistence, entities)
    correlation_id = flow_correlation_id(aircraft.id)

    if work.state == "in_progress":
        _dispatch(
            engine,
            work,
            "complete",
            key=("aviation-maintenance", work.id, "complete"),
            correlation_id=correlation_id,
        )
        work = _entity(
            persistence,
            "aviation_maintenance_work_order",
            work.id,
        )
    if work.state != "completed":
        raise RuntimeError("AOG maintenance is not complete or active")

    aircraft = _aircraft(persistence, entities)
    if aircraft.state == "maintenance":
        _dispatch(
            engine,
            aircraft,
            "finish_maintenance",
            key=("aviation-aircraft", aircraft.id, flight.id, "maintenance-complete"),
            correlation_id=correlation_id,
        )
        aircraft = _aircraft(persistence, entities)
    if aircraft.state != "released":
        raise RuntimeError("completed maintenance did not release aircraft")

    flight = _flight(persistence, flight_id)
    if flight.state == "inspection":
        _dispatch(
            engine,
            flight,
            "release",
            key=("aviation", flight.id, "maintenance-release"),
            correlation_id=correlation_id,
        )
    engine.preemptive_resources.withdraw(
        backend,
        f"maintenance-bay:{work.id}",
    )
    return _flight(persistence, flight_id).state == "released"


def preemption_result_for(persistence, *, preempting_request_id):
    return next(
        (
            result
            for result in persistence.resource_preemption_results()
            if result.preempting_request_id == preempting_request_id
        ),
        None,
    )
