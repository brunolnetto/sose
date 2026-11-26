"""MRO work-order completion and cancellation (split from simulation.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

if TYPE_CHECKING:
    from sose.backends.simpy import SimPyBackend
from .simulation_capacity import (
    release_capacity,
)
from .simulation_parts import (
    _part_issue_started,
)
from .simulation_runtime import (
    flow_correlation_id,
)

if TYPE_CHECKING:
    from .simulation import MROEntities


def reconcile_complete(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: MROEntities,
) -> None:
    wo = persistence.entity("work_order", entities.work_order_id)
    if wo is None:
        raise RuntimeError("work order was not persisted")
    if wo.state == "in_progress":
        engine.dispatch(
            engine.context.commands.create(
                "complete",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "complete"),
            )
        )
        wo = persistence.entity("work_order", entities.work_order_id)
    if wo.state == "completed":
        engine.dispatch(
            engine.context.commands.create(
                "close",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "close"),
            )
        )


def reconcile_cancel(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
) -> None:
    """Cancel outstanding work without leaving capacity or inventory side effects."""

    wo = persistence.entity("work_order", entities.work_order_id)
    demand = persistence.entity("part_demand", entities.part_demand_id)
    if wo is None or demand is None:
        raise RuntimeError("MRO entities were not persisted")
    if _part_issue_started(persistence):
        raise RuntimeError(
            "work order cannot be cancelled after spare-part issue has started"
        )

    if wo.state == "planned":
        engine.scheduler.cancel_pending(
            entity_type="work_order",
            entity_id=wo.id,
            name="release",
        )

    if wo.state in {"planned", "released", "waiting_material", "waiting_resource"}:
        engine.dispatch(
            engine.context.commands.create(
                "cancel",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "cancel"),
            )
        )

    demand = persistence.entity("part_demand", entities.part_demand_id)
    if demand.state in {"open", "waiting_inventory"}:
        engine.dispatch(
            engine.context.commands.create(
                "cancel",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "cancel"),
            )
        )

    release_capacity(persistence, engine, backend, entities=entities)
