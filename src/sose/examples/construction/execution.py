from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import ConstructionInspection, ConstructionMeasurement
from .runtime import (
    ConstructionEntities,
    activity,
    dispatch,
    flow_correlation_id,
    inspection,
    inspection_id,
    measurement_id,
)


def reconcile_dependency(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ConstructionEntities,
) -> bool:
    current = activity(persistence, entities.activity_id)
    predecessor = activity(persistence, entities.predecessor_id)
    completed = predecessor.state == "completed"
    correlation_id = flow_correlation_id(current.id)

    if current.state == "planned":
        event = "release" if completed else "block_dependency"
        dispatch(
            engine,
            current,
            event,
            key=("construction", current.id, event),
            correlation_id=correlation_id,
        )
        return completed

    if current.state == "blocked_dependency" and completed:
        dispatch(
            engine,
            current,
            "dependency_ready",
            key=("construction", current.id, "dependency-ready"),
            correlation_id=correlation_id,
        )
        return True

    return current.state != "blocked_dependency"


def complete_predecessor(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ConstructionEntities,
) -> None:
    predecessor = activity(persistence, entities.predecessor_id)
    if predecessor.state == "completed":
        return
    if predecessor.state != "measured":
        raise RuntimeError(
            f"predecessor is not at completion boundary: {predecessor.state}"
        )
    measurement = persistence.entity(
        "construction_measurement",
        measurement_id(predecessor.id),
    )
    if (
        measurement is None
        or measurement.state != "recorded"
        or float(measurement.attributes.get("value", 0.0)) <= 0
    ):
        raise RuntimeError(
            "predecessor completion requires durable measurement evidence"
        )
    dispatch(
        engine,
        predecessor,
        "complete",
        key=("construction-predecessor", predecessor.id, "complete"),
        correlation_id=flow_correlation_id(predecessor.id),
    )


def request_execution_resources(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ConstructionEntities,
    cycle: str = "initial",
) -> bool:
    current = activity(persistence, entities.activity_id)
    if current.state == "executing":
        return True

    if current.state == "rework":
        dispatch(
            engine,
            current,
            "schedule_rework",
            key=("construction", current.id, cycle, "schedule-rework"),
            correlation_id=flow_correlation_id(current.id),
        )
        current = activity(persistence, current.id)

    if current.state == "ready":
        if not bool(current.attributes.get("material_staged", False)):
            raise RuntimeError(
                "execution resources cannot be requested before material staging"
            )
        dispatch(
            engine,
            current,
            "request_resources",
            key=("construction", current.id, cycle, "request-resources"),
            correlation_id=flow_correlation_id(current.id),
        )
        current = activity(persistence, current.id)

    if current.state != "waiting_resource":
        return False

    crew_request = f"crew:{current.id}:{cycle}"
    equipment_request = f"equipment:{current.id}:{cycle}"

    if not engine.context.scenarios.attribute(
        "construction.site.available",
        True,
    ):
        # Capacity requests can outlive the context that made them legal.
        # Cancel queued demand first, then release any grant that raced with
        # the scenario change, so disrupted work cannot hoard capacity.
        for request_id in (crew_request, equipment_request):
            engine.resources.withdraw(backend, request_id)
        backend.run_until(backend.now)
        return False

    crew = engine.resources.ensure_requested(
        backend,
        resource_name="crew",
        request_id=crew_request,
        requested_at=backend.now,
    )
    equipment = engine.resources.ensure_requested(
        backend,
        resource_name="equipment",
        request_id=equipment_request,
        requested_at=backend.now,
    )

    if crew is None or equipment is None:
        if crew is not None:
            engine.resources.withdraw(backend, crew_request)
        if equipment is not None:
            engine.resources.withdraw(backend, equipment_request)
        return False

    current = activity(persistence, current.id)
    dispatch(
        engine,
        current,
        "start",
        key=("construction", current.id, cycle, "start"),
        correlation_id=flow_correlation_id(current.id),
    )
    return True


def finish_execution(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ConstructionEntities,
    cycle: str = "initial",
) -> None:
    current = activity(persistence, entities.activity_id)
    if current.state == "executing":
        dispatch(
            engine,
            current,
            "finish_work",
            key=("construction", current.id, cycle, "finish-work"),
            correlation_id=flow_correlation_id(current.id),
        )
        current = activity(persistence, current.id)
    elif current.state != "inspection":
        raise RuntimeError(f"activity is not at finish boundary: {current.state}")

    # Cleanup is deliberately post-state idempotent. If finish_work committed
    # before a crash, restart sees Activity(inspection) plus durable resource
    # reservations and can still finish releasing them.
    for request_id in (
        f"crew:{current.id}:{cycle}",
        f"equipment:{current.id}:{cycle}",
    ):
        engine.resources.withdraw(backend, request_id)


def ensure_inspection(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ConstructionEntities,
    ordinal: int,
) -> ConstructionInspection:
    current = activity(persistence, entities.activity_id)
    if current.state != "inspection":
        raise RuntimeError(
            f"activity is not waiting for inspection: {current.state}"
        )

    existing = inspection(
        persistence,
        current.id,
        ordinal,
    )
    if existing is not None:
        return existing

    created = engine.context.entities.create(
        ConstructionInspection,
        key=(
            "construction-reference",
            current.id,
            "inspection",
            ordinal,
        ),
        state="pending",
        attributes={
            "activity_id": current.id,
            "ordinal": ordinal,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(created)
    return created


def reconcile_inspection(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ConstructionEntities,
    ordinal: int,
    outcome: str,
) -> bool:
    if outcome not in {"pass", "fail"}:
        raise ValueError(f"unsupported inspection outcome: {outcome}")

    current = activity(persistence, entities.activity_id)
    occurrence = inspection(
        persistence,
        current.id,
        ordinal,
    )

    # Acceptance/rejection may commit before inspector release. Reconciliation
    # must still complete cleanup from the advanced Activity state.
    if current.state != "inspection":
        if occurrence is None or occurrence.state not in {"passed", "failed"}:
            return False
        request_id = f"inspector:{occurrence.id}"
        engine.resources.withdraw(backend, request_id)
        expected_activity = "measured" if occurrence.state == "passed" else "rework"
        return current.state == expected_activity

    occurrence = ensure_inspection(
        persistence,
        engine,
        entities=entities,
        ordinal=ordinal,
    )
    request_id = f"inspector:{occurrence.id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="inspector",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(current.id)
    occurrence = inspection(persistence, current.id, ordinal)
    if occurrence is None:
        raise RuntimeError("inspection occurrence disappeared")
    if occurrence.state == "pending":
        dispatch(
            engine,
            occurrence,
            "begin",
            key=("construction-inspection", occurrence.id, "begin"),
            correlation_id=correlation_id,
        )

    occurrence = inspection(persistence, current.id, ordinal)
    if occurrence is None:
        raise RuntimeError("inspection occurrence disappeared")
    result_event = "pass_inspection" if outcome == "pass" else "fail_inspection"
    if occurrence.state == "inspecting":
        dispatch(
            engine,
            occurrence,
            result_event,
            key=("construction-inspection", occurrence.id, result_event),
            correlation_id=correlation_id,
        )

    occurrence = inspection(persistence, current.id, ordinal)
    if occurrence is None:
        raise RuntimeError("inspection occurrence disappeared")
    expected = "passed" if outcome == "pass" else "failed"
    if occurrence.state != expected:
        raise RuntimeError(
            f"inspection did not reach expected outcome: {occurrence.state}"
        )

    current = activity(persistence, current.id)
    activity_event = "accept" if outcome == "pass" else "reject"
    dispatch(
        engine,
        current,
        activity_event,
        key=(
            "construction",
            current.id,
            occurrence.id,
            activity_event,
        ),
        correlation_id=correlation_id,
    )
    engine.resources.withdraw(backend, request_id)
    return True


def record_measurement(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ConstructionEntities,
    value: float,
) -> ConstructionMeasurement:
    if value <= 0:
        raise ValueError("measurement must be positive")
    current = activity(persistence, entities.activity_id)
    if current.state != "measured":
        raise RuntimeError(
            f"measurement requires Activity(measured), got {current.state}"
        )

    existing = persistence.entity(
        "construction_measurement",
        measurement_id(current.id),
    )
    if existing is not None:
        return existing

    measurement = engine.context.entities.create(
        ConstructionMeasurement,
        key=(
            "construction-reference",
            current.id,
            "measurement",
        ),
        state="recorded",
        attributes={
            "activity_id": current.id,
            "value": float(value),
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(measurement)
    return measurement


def complete_activity(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ConstructionEntities,
) -> None:
    current = activity(persistence, entities.activity_id)
    if current.state == "completed":
        return
    if current.state != "measured":
        raise RuntimeError(
            f"activity is not at completion boundary: {current.state}"
        )
    measurement = persistence.entity(
        "construction_measurement",
        measurement_id(current.id),
    )
    if (
        measurement is None
        or measurement.state != "recorded"
        or float(measurement.attributes.get("value", 0.0)) <= 0
    ):
        raise RuntimeError(
            "activity completion requires durable measurement evidence"
        )

    dispatch(
        engine,
        current,
        "complete",
        key=("construction", current.id, "complete"),
        correlation_id=flow_correlation_id(current.id),
    )
