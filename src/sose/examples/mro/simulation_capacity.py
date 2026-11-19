"""MRO maintenance-bay capacity request, grant discard and release (split from simulation.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

if TYPE_CHECKING:
    from sose.backends.simpy import SimPyBackend

if TYPE_CHECKING:
    from .simulation import MROEntities


def _discard_granted_capacity_if_terminal(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: "SimPyBackend",
    *,
    work_order_id: str,
    reservation,
    preemptive: bool,
) -> None:
    work_order = persistence.entity("work_order", work_order_id)
    if work_order is None or work_order.state not in {"cancelled", "closed"}:
        return
    if preemptive:
        engine.preemptive_resources.withdraw(backend, reservation.request_id)
    else:
        engine.resources.withdraw(backend, reservation.request_id)


def reconcile_capacity(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
) -> bool:
    if not engine.context.scenarios.attribute("mro.technician.available", True):
        return False

    tech_id = f"technician:{entities.work_order_id}"
    bay_id = f"bay:{entities.work_order_id}"

    engine.resources.ensure_requested(
        backend,
        resource_name="technician",
        request_id=tech_id,
        requested_at=backend.now,
        priority=100,
        on_acquired=lambda reservation: _discard_granted_capacity_if_terminal(
            persistence,
            engine,
            backend,
            work_order_id=entities.work_order_id,
            reservation=reservation,
            preemptive=False,
        ),
    )
    engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="maintenance_bay",
        request_id=bay_id,
        requested_at=backend.now,
        priority=100,
        preempt=False,
        on_acquired=lambda reservation: _discard_granted_capacity_if_terminal(
            persistence,
            engine,
            backend,
            work_order_id=entities.work_order_id,
            reservation=reservation,
            preemptive=True,
        ),
    )

    return (
        engine.resources.reservation_for(tech_id) is not None
        and engine.preemptive_resources.reservation_for(bay_id) is not None
    )


def release_capacity(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
) -> None:
    engine.resources.withdraw(
        backend,
        f"technician:{entities.work_order_id}",
    )
    engine.preemptive_resources.withdraw(
        backend,
        f"bay:{entities.work_order_id}",
    )
