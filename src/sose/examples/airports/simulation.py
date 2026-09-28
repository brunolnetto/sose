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

from .entities import (
    BaggageFlow,
    DepartureSlot,
    FlightTurnaround,
    GateAssignment,
    GroundServiceTask,
)
from .scenarios import ORIGIN
from .statecharts import (
    BaggageFlowChart,
    DepartureSlotChart,
    FlightTurnaroundChart,
    GateAssignmentChart,
    GroundServiceTaskChart,
)


ARRIVAL_DELAY = timedelta(hours=1)
DEPARTURE_SLOT_DELAY = timedelta(hours=4)


@dataclass(frozen=True, slots=True)
class AirportEntities:
    turnaround_id: str
    gate_assignment_id: str
    service_task_id: str
    baggage_flow_id: str
    departure_slot_id: str


def flow_correlation_id(turnaround_id: str) -> str:
    return deterministic_id("airport-turnaround-flow", turnaround_id)


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 612,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("airport_flight_turnaround", FlightTurnaroundChart))
    registry.register(EntityType("airport_gate_assignment", GateAssignmentChart))
    registry.register(EntityType("airport_ground_service_task", GroundServiceTaskChart))
    registry.register(EntityType("airport_baggage_flow", BaggageFlowChart))
    registry.register(EntityType("airport_departure_slot", DepartureSlotChart))
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
    flight_number: str = "SOSE101",
    departure_priority: int = 50,
    gate: str = "G1",
    gate_capacity: int = 1,
    ground_team_capacity: int = 1,
    tug_capacity: int = 1,
    departure_queue_capacity: int = 100,
) -> AirportEntities:
    context, engine = build_runtime(persistence, now=now)
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


def _entity(persistence, entity_type, entity_id):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _turnaround(persistence, entities):
    return _entity(
        persistence,
        "airport_flight_turnaround",
        entities.turnaround_id,
    )


def _gate_assignment(persistence, entities):
    return _entity(
        persistence,
        "airport_gate_assignment",
        entities.gate_assignment_id,
    )


def _service_task(persistence, entities):
    return _entity(
        persistence,
        "airport_ground_service_task",
        entities.service_task_id,
    )


def _baggage(persistence, entities):
    return _entity(
        persistence,
        "airport_baggage_flow",
        entities.baggage_flow_id,
    )


def _slot(persistence, entities):
    return _entity(
        persistence,
        "airport_departure_slot",
        entities.departure_slot_id,
    )


def _dispatch(engine, entity, event, *, key, correlation_id):
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def schedule_arrival(
    persistence,
    engine,
    backend,
    *,
    entities,
    delay=ARRIVAL_DELAY,
):
    turnaround = _turnaround(persistence, entities)
    if turnaround.state != "scheduled":
        return backend.now
    existing = engine.scheduler.find_pending(
        entity_type="airport_flight_turnaround",
        entity_id=turnaround.id,
        name="arrive",
    )
    if existing is not None:
        return existing.work.due_at
    due_at = backend.now + delay
    command = engine.context.commands.create(
        "arrive",
        target=turnaround,
        due_at=due_at,
        correlation_id=flow_correlation_id(turnaround.id),
        key=("airport", turnaround.id, "arrival"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def reconcile_gate(
    persistence,
    engine,
    backend,
    *,
    entities,
):
    turnaround = _turnaround(persistence, entities)
    assignment = _gate_assignment(persistence, entities)
    request_id = f"gate:{turnaround.id}"

    if assignment.state in {"occupied", "released"}:
        if turnaround.state in {"arrived", "gate_hold"}:
            _dispatch(
                engine,
                turnaround,
                "assign_gate",
                key=("airport", turnaround.id, assignment.id, "assign-gate"),
                correlation_id=flow_correlation_id(turnaround.id),
            )
        return True

    if turnaround.state not in {"arrived", "gate_hold"}:
        return False

    # A gate reallocation can commit its semantic state before the old gate
    # reservation is released. Never reuse that stale reservation as ownership
    # of the replacement gate.
    if assignment.state == "reallocated":
        engine.resources.withdraw(backend, request_id)

    if not engine.context.scenarios.attribute("airport.gate.available", True):
        engine.resources.withdraw(backend, request_id)
        if turnaround.state == "arrived":
            _dispatch(
                engine,
                turnaround,
                "hold_gate",
                key=("airport", turnaround.id, "gate-hold"),
                correlation_id=flow_correlation_id(turnaround.id),
            )
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="gate",
        request_id=request_id,
        requested_at=backend.now,
        priority=int(turnaround.attributes["departure_priority"]),
    )
    if reservation is None:
        if turnaround.state == "arrived":
            _dispatch(
                engine,
                turnaround,
                "hold_gate",
                key=("airport", turnaround.id, "gate-hold"),
                correlation_id=flow_correlation_id(turnaround.id),
            )
        return False

    correlation_id = flow_correlation_id(turnaround.id)
    assignment = _gate_assignment(persistence, entities)
    if assignment.state in {"planned", "reallocated"}:
        _dispatch(
            engine,
            assignment,
            "reserve",
            key=("airport-gate", assignment.id, "reserve"),
            correlation_id=correlation_id,
        )
        assignment = _gate_assignment(persistence, entities)
    if assignment.state == "reserved":
        _dispatch(
            engine,
            assignment,
            "occupy",
            key=("airport-gate", assignment.id, "occupy"),
            correlation_id=correlation_id,
        )
    turnaround = _turnaround(persistence, entities)
    if turnaround.state in {"arrived", "gate_hold"}:
        _dispatch(
            engine,
            turnaround,
            "assign_gate",
            key=("airport", turnaround.id, assignment.id, "assign-gate"),
            correlation_id=correlation_id,
        )
    return True


def reallocate_gate(
    persistence,
    engine,
    backend,
    *,
    entities,
    new_gate: str,
) -> bool:
    turnaround = _turnaround(persistence, entities)
    assignment = _gate_assignment(persistence, entities)
    if turnaround.state != "gate_assigned" or assignment.state != "occupied":
        raise RuntimeError(
            "gate reallocation requires assigned turnaround and occupied gate"
        )

    correlation_id = flow_correlation_id(turnaround.id)
    _dispatch(
        engine,
        turnaround,
        "reallocate_gate",
        key=("airport", turnaround.id, assignment.id, "reallocate-gate"),
        correlation_id=correlation_id,
    )
    _dispatch(
        engine,
        assignment,
        "reallocate",
        key=("airport-gate", assignment.id, "reallocate"),
        correlation_id=correlation_id,
    )
    engine.resources.withdraw(backend, f"gate:{turnaround.id}")

    assignment = _gate_assignment(persistence, entities)
    assignment.attributes["gate"] = new_gate
    with persistence.transaction() as uow:
        uow.save_entity(assignment)

    return reconcile_gate(
        persistence,
        engine,
        backend,
        entities=entities,
    )


def reconcile_ground_service(
    persistence,
    engine,
    backend,
    *,
    entities,
):
    turnaround = _turnaround(persistence, entities)
    task = _service_task(persistence, entities)
    request_id = f"ground-team:{task.id}"

    if task.state == "completed":
        engine.resources.withdraw(backend, request_id)
        turnaround = _turnaround(persistence, entities)
        if turnaround.state == "servicing":
            _dispatch(
                engine,
                turnaround,
                "service_ready",
                key=("airport", turnaround.id, task.id, "service-ready"),
                correlation_id=flow_correlation_id(turnaround.id),
            )
        return True

    if turnaround.state == "gate_assigned":
        _dispatch(
            engine,
            turnaround,
            "start_deboarding",
            key=("airport", turnaround.id, "deboarding"),
            correlation_id=flow_correlation_id(turnaround.id),
        )
        turnaround = _turnaround(persistence, entities)
    if turnaround.state == "deboarding":
        _dispatch(
            engine,
            turnaround,
            "start_servicing",
            key=("airport", turnaround.id, "start-servicing"),
            correlation_id=flow_correlation_id(turnaround.id),
        )
    turnaround = _turnaround(persistence, entities)
    if turnaround.state != "servicing":
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="ground_team",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    task = _service_task(persistence, entities)
    correlation_id = flow_correlation_id(turnaround.id)
    if task.state == "pending":
        _dispatch(
            engine,
            task,
            "start",
            key=("airport-service", task.id, "start"),
            correlation_id=correlation_id,
        )
        task = _service_task(persistence, entities)
    if task.state == "in_progress":
        _dispatch(
            engine,
            task,
            "complete",
            key=("airport-service", task.id, "complete"),
            correlation_id=correlation_id,
        )
    return reconcile_ground_service(
        persistence,
        engine,
        backend,
        entities=entities,
    )


def reconcile_baggage(
    persistence,
    engine,
    *,
    entities,
    delayed=False,
):
    turnaround = _turnaround(persistence, entities)
    baggage = _baggage(persistence, entities)
    if turnaround.state not in {"boarding", "waiting_baggage"}:
        return False
    correlation_id = flow_correlation_id(turnaround.id)

    # The baggage delay may commit before the turnaround wait-state. Preserve
    # that operational fact before allowing recovery to mark baggage ready.
    if baggage.state == "delayed" and turnaround.state == "boarding":
        _dispatch(
            engine,
            turnaround,
            "baggage_delayed",
            key=("airport", turnaround.id, baggage.id, "baggage-delayed"),
            correlation_id=correlation_id,
        )
        turnaround = _turnaround(persistence, entities)

    if baggage.state == "pending":
        _dispatch(
            engine,
            baggage,
            "start",
            key=("airport-baggage", baggage.id, "start"),
            correlation_id=correlation_id,
        )
        baggage = _baggage(persistence, entities)

    if delayed and baggage.state == "transferring":
        _dispatch(
            engine,
            baggage,
            "delay",
            key=("airport-baggage", baggage.id, "delay"),
            correlation_id=correlation_id,
        )
        if turnaround.state == "boarding":
            _dispatch(
                engine,
                turnaround,
                "baggage_delayed",
                key=("airport", turnaround.id, baggage.id, "baggage-delayed"),
                correlation_id=correlation_id,
            )
        return False

    if baggage.state in {"transferring", "delayed"}:
        _dispatch(
            engine,
            baggage,
            "mark_ready",
            key=("airport-baggage", baggage.id, "mark-ready"),
            correlation_id=correlation_id,
        )
    turnaround = _turnaround(persistence, entities)
    if turnaround.state == "waiting_baggage":
        _dispatch(
            engine,
            turnaround,
            "baggage_ready",
            key=("airport", turnaround.id, baggage.id, "baggage-ready"),
            correlation_id=correlation_id,
        )
    return True


def schedule_departure_slot(
    persistence,
    engine,
    backend,
    *,
    entities,
    delay=DEPARTURE_SLOT_DELAY,
):
    slot = _slot(persistence, entities)
    if slot.state == "planned":
        _dispatch(
            engine,
            slot,
            "schedule",
            key=("airport-slot", slot.id, "schedule"),
            correlation_id=flow_correlation_id(entities.turnaround_id),
        )
        slot = _slot(persistence, entities)
    existing = engine.scheduler.find_pending(
        entity_type="airport_departure_slot",
        entity_id=slot.id,
        name="make_due",
    )
    if slot.state == "scheduled" and existing is None:
        due_at = backend.now + delay
        command = engine.context.commands.create(
            "make_due",
            target=slot,
            due_at=due_at,
            correlation_id=flow_correlation_id(entities.turnaround_id),
            key=("airport-slot", slot.id, "due"),
        )
        engine.context.schedules.at(due_at, command=command)
        return due_at
    if existing is not None:
        return existing.work.due_at
    return backend.now


def queue_departure(
    persistence,
    engine,
    backend,
    *,
    entities,
):
    turnaround = _turnaround(persistence, entities)
    baggage = _baggage(persistence, entities)
    task = _service_task(persistence, entities)
    if turnaround.state not in {"boarding", "waiting_slot"}:
        raise RuntimeError(
            "departure queue requires FlightTurnaround(boarding/waiting_slot), "
            f"got {turnaround.state}"
        )
    if baggage.state != "ready":
        raise RuntimeError("departure queue requires BaggageFlow(ready)")
    if task.state != "completed":
        raise RuntimeError("departure queue requires GroundServiceTask(completed)")

    if turnaround.state == "boarding":
        _dispatch(
            engine,
            turnaround,
            "start_boarding",
            key=("airport", turnaround.id, "boarding-complete"),
            correlation_id=flow_correlation_id(turnaround.id),
        )
    item_id = f"departure:{turnaround.id}"
    queued = any(item.item_id == item_id for item in persistence.store_items())
    consumed = any(
        result.item.item_id == item_id
        for result in persistence.store_get_results()
    )
    pending = any(intent.item_id == item_id for intent in persistence.store_put_intents())
    if not (queued or consumed or pending):
        engine.stores.put(
            backend,
            store_name="departure_queue",
            item_id=item_id,
            value={"turnaround_id": turnaround.id},
            priority=int(turnaround.attributes["departure_priority"]),
            requested_at=backend.now,
        )
        backend.run_until(backend.now)


def reconcile_departure(
    persistence,
    engine,
    backend,
    *,
    entities,
    dispatcher_id="departure",
):
    turnaround = _turnaround(persistence, entities)
    slot = _slot(persistence, entities)
    gate = _gate_assignment(persistence, entities)
    request_id = f"tug:{turnaround.id}"

    if turnaround.state == "departed":
        engine.resources.withdraw(backend, request_id)
        engine.resources.withdraw(backend, f"gate:{turnaround.id}")
        if gate.state == "occupied":
            _dispatch(
                engine,
                gate,
                "release",
                key=("airport-gate", gate.id, "release"),
                correlation_id=flow_correlation_id(turnaround.id),
            )
        return True

    # slot_ready / slot consumption may commit before the final departure
    # transition. Recovery must finish pushback without requiring a fresh slot
    # or a second departure-queue item.
    if turnaround.state == "pushback":
        _dispatch(
            engine,
            turnaround,
            "depart",
            key=("airport", turnaround.id, "depart"),
            correlation_id=flow_correlation_id(turnaround.id),
        )
        return reconcile_departure(
            persistence,
            engine,
            backend,
            entities=entities,
            dispatcher_id=dispatcher_id,
        )

    if turnaround.state != "waiting_slot":
        return False
    if slot.state not in {"due", "delayed"}:
        return False

    if not engine.context.scenarios.attribute("airport.departure.available", True):
        if slot.state == "due":
            _dispatch(
                engine,
                slot,
                "delay",
                key=("airport-slot", slot.id, "weather-delay"),
                correlation_id=flow_correlation_id(turnaround.id),
            )
        engine.resources.withdraw(backend, request_id)
        return False

    tug = engine.resources.ensure_requested(
        backend,
        resource_name="tug",
        request_id=request_id,
        requested_at=backend.now,
        priority=int(turnaround.attributes["departure_priority"]),
    )
    if tug is None:
        return False

    get_id = f"departure-pick:{dispatcher_id}"
    result = engine.stores.selection(get_id)

    # Priority ownership remains domain-visible: only the current durable head
    # may initiate selection. Once StoreGetResult commits, recovery continues
    # from that result even though the queue item is gone.
    if result is None:
        queued_items = sorted(
            (
                item
                for item in persistence.store_items()
                if item.store_name == "departure_queue"
            ),
            key=lambda item: (item.priority, item.sequence, item.item_id),
        )
        if not queued_items:
            engine.resources.withdraw(backend, request_id)
            return False
        if str(queued_items[0].value["turnaround_id"]) != turnaround.id:
            engine.resources.withdraw(backend, request_id)
            return False

        result = engine.stores.ensure_selection(
            backend,
            store_name="departure_queue",
            request_id=get_id,
            requested_at=backend.now,
        )
    if result is None:
        engine.resources.withdraw(backend, request_id)
        return False

    selected = str(result.item.value["turnaround_id"])
    if selected != turnaround.id:
        engine.resources.withdraw(backend, request_id)
        return False

    correlation_id = flow_correlation_id(turnaround.id)
    turnaround = _turnaround(persistence, entities)
    if turnaround.state == "waiting_slot":
        _dispatch(
            engine,
            turnaround,
            "slot_ready",
            key=("airport", turnaround.id, slot.id, "slot-ready"),
            correlation_id=correlation_id,
        )
    slot = _slot(persistence, entities)
    if slot.state in {"due", "delayed"}:
        _dispatch(
            engine,
            slot,
            "consume",
            key=("airport-slot", slot.id, "consume"),
            correlation_id=correlation_id,
        )
    turnaround = _turnaround(persistence, entities)
    if turnaround.state == "pushback":
        _dispatch(
            engine,
            turnaround,
            "depart",
            key=("airport", turnaround.id, "depart"),
            correlation_id=correlation_id,
        )
    return reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        dispatcher_id=dispatcher_id,
    )
