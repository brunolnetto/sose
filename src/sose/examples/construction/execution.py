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
    measurement_id,
)
from .execution_resources import (  # noqa: F401 - historical import location
    finish_execution,
    request_execution_resources,
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
        return _reconcile_inspection_after_transition(
            engine,
            backend,
            current_state=current.state,
            occurrence=occurrence,
        )

    occurrence = ensure_inspection(
        persistence,
        engine,
        entities=entities,
        ordinal=ordinal,
    )
    request_id = f"inspector:{occurrence.id}"
    if engine.resources.ensure_requested(
        backend,
        resource_name="inspector",
        request_id=request_id,
        requested_at=backend.now,
    ) is None:
        return False

    correlation_id = flow_correlation_id(current.id)
    occurrence = _inspection_or_error(
        persistence,
        current_id=current.id,
        ordinal=ordinal,
    )
    _begin_inspection_if_pending(
        engine,
        occurrence,
        correlation_id=correlation_id,
    )
    occurrence = _inspection_or_error(
        persistence,
        current_id=current.id,
        ordinal=ordinal,
    )
    _dispatch_outcome_if_inspecting(
        engine,
        occurrence,
        outcome=outcome,
        correlation_id=correlation_id,
    )
    occurrence = _inspection_or_error(
        persistence,
        current_id=current.id,
        ordinal=ordinal,
    )
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


def _reconcile_inspection_after_transition(
    engine: Engine,
    backend: SimPyBackend,
    *,
    current_state: str,
    occurrence: ConstructionInspection | None,
) -> bool:
    if occurrence is None or occurrence.state not in {"passed", "failed"}:
        return False
    request_id = f"inspector:{occurrence.id}"
    engine.resources.withdraw(backend, request_id)
    expected_activity = "measured" if occurrence.state == "passed" else "rework"
    return current_state == expected_activity


def _inspection_or_error(
    persistence: MemoryPersistence,
    *,
    current_id: str,
    ordinal: int,
) -> ConstructionInspection:
    occurrence = inspection(persistence, current_id, ordinal)
    if occurrence is None:
        raise RuntimeError("inspection occurrence disappeared")
    return occurrence


def _begin_inspection_if_pending(
    engine: Engine,
    occurrence: ConstructionInspection,
    *,
    correlation_id: str,
) -> None:
    if occurrence.state != "pending":
        return
    dispatch(
        engine,
        occurrence,
        "begin",
        key=("construction-inspection", occurrence.id, "begin"),
        correlation_id=correlation_id,
    )


def _dispatch_outcome_if_inspecting(
    engine: Engine,
    occurrence: ConstructionInspection,
    *,
    outcome: str,
    correlation_id: str,
) -> None:
    if occurrence.state != "inspecting":
        return
    result_event = "pass_inspection" if outcome == "pass" else "fail_inspection"
    dispatch(
        engine,
        occurrence,
        result_event,
        key=("construction-inspection", occurrence.id, result_event),
        correlation_id=correlation_id,
    )


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


# Moved by the quality-ratchet splits (#632); importing these names from here
# is deprecated (ADR-0011). Import them from the module named on the right.
from sose._deprecation import deprecated_reexports as _deprecated_reexports

__getattr__ = _deprecated_reexports(__name__, {
    "inspection_id": "sose.examples.construction.runtime",
}, since="0.8.1", removed_in="0.9.0")
