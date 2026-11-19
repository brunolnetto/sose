"""Airport arrival scheduling and gate allocation (split from simulation.py)."""

from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine

from .simulation_runtime import (
    ARRIVAL_DELAY,
    _dispatch,
    _gate_assignment,
    _turnaround,
    flow_correlation_id,
)


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
    correlation_id = flow_correlation_id(turnaround.id)

    if assignment.state in {"occupied", "released"}:
        _dispatch_assign_gate_if_waiting(
            engine,
            turnaround=turnaround,
            assignment_id=assignment.id,
            correlation_id=correlation_id,
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
        _release_gate_and_hold_if_arrived(
            engine,
            backend,
            turnaround=turnaround,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        return False

    if engine.resources.ensure_requested(
        backend,
        resource_name="gate",
        request_id=request_id,
        requested_at=backend.now,
        priority=int(turnaround.attributes["departure_priority"]),
    ) is None:
        _hold_gate_if_arrived(
            engine,
            turnaround=turnaround,
            correlation_id=correlation_id,
        )
        return False

    assignment = _progress_gate_assignment(
        persistence,
        engine,
        entities=entities,
        correlation_id=correlation_id,
    )
    turnaround = _turnaround(persistence, entities)
    _dispatch_assign_gate_if_waiting(
        engine,
        turnaround=turnaround,
        assignment_id=assignment.id,
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


def _dispatch_assign_gate_if_waiting(
    engine: Engine,
    *,
    turnaround,
    assignment_id: str,
    correlation_id: str,
) -> None:
    if turnaround.state not in {"arrived", "gate_hold"}:
        return
    _dispatch(
        engine,
        turnaround,
        "assign_gate",
        key=("airport", turnaround.id, assignment_id, "assign-gate"),
        correlation_id=correlation_id,
    )


def _release_gate_and_hold_if_arrived(
    engine: Engine,
    backend: SimPyBackend,
    *,
    turnaround,
    request_id: str,
    correlation_id: str,
) -> None:
    engine.resources.withdraw(backend, request_id)
    if turnaround.state != "arrived":
        return
    _dispatch(
        engine,
        turnaround,
        "hold_gate",
        key=("airport", turnaround.id, "gate-hold"),
        correlation_id=correlation_id,
    )


def _hold_gate_if_arrived(
    engine: Engine,
    *,
    turnaround,
    correlation_id: str,
) -> None:
    if turnaround.state != "arrived":
        return
    _dispatch(
        engine,
        turnaround,
        "hold_gate",
        key=("airport", turnaround.id, "gate-hold"),
        correlation_id=correlation_id,
    )


def _progress_gate_assignment(
    persistence,
    engine,
    *,
    entities,
    correlation_id: str,
):
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
    return assignment
