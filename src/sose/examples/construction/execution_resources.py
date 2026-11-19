"""Construction crew/equipment requests and execution finish (split from execution.py).

``activity``, ``dispatch`` and ``flow_correlation_id`` are resolved through
``execution`` at call time so monkeypatches applied to that module still apply.
"""

from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .runtime import ConstructionEntities


def _execution():
    from . import execution

    return execution


def _execution_request_ids(activity_id: str, cycle: str) -> tuple[str, str]:
    return (f"crew:{activity_id}:{cycle}", f"equipment:{activity_id}:{cycle}")


def _advance_rework_if_needed(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    current_id: str,
    cycle: str,
):
    current = _execution().activity(persistence, current_id)
    if current.state != "rework":
        return current
    _execution().dispatch(
        engine,
        current,
        "schedule_rework",
        key=("construction", current.id, cycle, "schedule-rework"),
        correlation_id=_execution().flow_correlation_id(current.id),
    )
    return _execution().activity(persistence, current.id)


def _request_resources_if_ready(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    current_id: str,
    cycle: str,
):
    current = _execution().activity(persistence, current_id)
    if current.state != "ready":
        return current
    if not bool(current.attributes.get("material_staged", False)):
        raise RuntimeError(
            "execution resources cannot be requested before material staging"
        )
    _execution().dispatch(
        engine,
        current,
        "request_resources",
        key=("construction", current.id, cycle, "request-resources"),
        correlation_id=_execution().flow_correlation_id(current.id),
    )
    return _execution().activity(persistence, current.id)


def _withdraw_execution_requests(
    engine: Engine,
    backend: SimPyBackend,
    *,
    request_ids: tuple[str, str],
) -> None:
    for request_id in request_ids:
        engine.resources.withdraw(backend, request_id)


def _ensure_execution_capacity(
    engine: Engine,
    backend: SimPyBackend,
    *,
    current_id: str,
    request_ids: tuple[str, str],
) -> bool:
    crew_request, equipment_request = request_ids
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
    return True


def request_execution_resources(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ConstructionEntities,
    cycle: str = "initial",
) -> bool:
    current = _execution().activity(persistence, entities.activity_id)
    if current.state == "executing":
        return True
    request_ids = _execution_request_ids(current.id, cycle)

    current = _advance_rework_if_needed(
        persistence,
        engine,
        current_id=current.id,
        cycle=cycle,
    )
    current = _request_resources_if_ready(
        persistence,
        engine,
        current_id=current.id,
        cycle=cycle,
    )

    if current.state != "waiting_resource":
        return False

    if not engine.context.scenarios.attribute(
        "construction.site.available",
        True,
    ):
        # Capacity requests can outlive the context that made them legal.
        # Cancel queued demand first, then release any grant that raced with
        # the scenario change, so disrupted work cannot hoard capacity.
        _withdraw_execution_requests(
            engine,
            backend,
            request_ids=request_ids,
        )
        return False

    if not _ensure_execution_capacity(
        engine,
        backend,
        current_id=current.id,
        request_ids=request_ids,
    ):
        return False

    current = _execution().activity(persistence, current.id)
    _execution().dispatch(
        engine,
        current,
        "start",
        key=("construction", current.id, cycle, "start"),
        correlation_id=_execution().flow_correlation_id(current.id),
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
    current = _execution().activity(persistence, entities.activity_id)
    if current.state == "executing":
        _execution().dispatch(
            engine,
            current,
            "finish_work",
            key=("construction", current.id, cycle, "finish-work"),
            correlation_id=_execution().flow_correlation_id(current.id),
        )
        current = _execution().activity(persistence, current.id)
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
