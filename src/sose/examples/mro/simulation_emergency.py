"""MRO emergency preemption bookkeeping and resume (split from simulation.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

if TYPE_CHECKING:
    from sose.backends.simpy import SimPyBackend
from .simulation_runtime import (
    flow_correlation_id,
)

if TYPE_CHECKING:
    from .simulation import MROEntities


def _active_emergency_request_id(
    persistence: MemoryPersistence,
    work_order_id: str,
    *,
    prefix: str | None = None,
) -> str | None:
    prefix = prefix or f"bay-emergency:{work_order_id}:"
    for demand in persistence.preemptive_resource_demands():
        if demand.request_id.startswith(prefix):
            return demand.request_id
    for reservation in persistence.preemptive_resource_reservations():
        if reservation.request_id.startswith(prefix):
            return reservation.request_id
    return None


def _committed_emergency_result(
    persistence: MemoryPersistence,
    work_order_id: str,
    *,
    prefix: str | None = None,
):
    normal_id = f"bay:{work_order_id}"
    prefix = prefix or f"bay-emergency:{work_order_id}:"
    matches = [
        result
        for result in persistence.resource_preemption_results()
        if result.displaced_request_id == normal_id
        and result.preempting_request_id.startswith(prefix)
    ]
    if not matches:
        return None

    active_request_id = _active_emergency_request_id(
        persistence,
        work_order_id,
        prefix=prefix,
    )
    if active_request_id is not None:
        active_matches = [
            result
            for result in matches
            if result.preempting_request_id == active_request_id
        ]
        if active_matches:
            return max(active_matches, key=lambda result: result.sequence)

    return max(matches, key=lambda result: result.sequence)


def _next_emergency_request_id(
    persistence: MemoryPersistence,
    work_order_id: str,
    *,
    prefix: str | None = None,
) -> str:
    prefix = prefix or f"bay-emergency:{work_order_id}:"
    active = _active_emergency_request_id(
        persistence, work_order_id, prefix=prefix
    )
    if active is not None:
        return active
    occurrence = 1 + sum(
        result.preempting_request_id.startswith(prefix)
        for result in persistence.resource_preemption_results()
    )
    return f"{prefix}{occurrence}"


def _reconcile_committed_emergency_interrupt(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    wo,
    work_order_id: str,
    request_prefix: str | None,
) -> bool:
    active_emergency_id = _active_emergency_request_id(
        persistence,
        work_order_id,
        prefix=request_prefix,
    )
    if active_emergency_id is None:
        return False
    committed = _committed_emergency_result(
        persistence,
        work_order_id,
        prefix=request_prefix,
    )
    if committed is None or committed.preempting_request_id != active_emergency_id:
        return False
    _dispatch_work_order_interrupt(
        engine,
        wo=wo,
        emergency_id=committed.preempting_request_id,
    )
    return True


def _dispatch_work_order_interrupt(
    engine: Engine,
    *,
    wo,
    emergency_id: str,
) -> None:
    if wo is None or wo.state != "in_progress":
        return
    engine.dispatch(
        engine.context.commands.create(
            "interrupt",
            target=wo,
            correlation_id=flow_correlation_id(),
            key=("mro-emergency", wo.id, emergency_id, "interrupt"),
        )
    )


def reconcile_emergency_resume(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    request_prefix: str | None = None,
) -> bool:
    """Release emergency capacity and reacquire the bay only for interrupted work."""

    wo = persistence.entity("work_order", entities.work_order_id)
    if wo is None:
        raise RuntimeError("work order was not persisted")
    if wo.state == "in_progress":
        return True
    if wo.state != "interrupted":
        return False

    emergency_id = _active_emergency_request_id(
        persistence,
        entities.work_order_id,
        prefix=request_prefix,
    )
    normal_id = f"bay:{entities.work_order_id}"

    if emergency_id is not None:
        engine.preemptive_resources.withdraw(backend, emergency_id)

    normal = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="maintenance_bay",
        request_id=normal_id,
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    if normal is None:
        return False

    wo = persistence.entity("work_order", entities.work_order_id)
    if wo is not None and wo.state == "interrupted":
        engine.dispatch(
            engine.context.commands.create(
                "resume",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro-emergency", wo.id, emergency_id or "none", "resume"),
            )
        )
    return persistence.entity("work_order", entities.work_order_id).state == "in_progress"
