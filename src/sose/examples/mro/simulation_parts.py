"""MRO spare-part stock, material availability and part issue (split from simulation.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

if TYPE_CHECKING:
    from sose.backends.simpy import SimPyBackend
from .simulation_capacity import (
    release_capacity,
)
from .simulation_runtime import (
    PART_CAPACITY,
    PART_SKU,
    flow_correlation_id,
)

if TYPE_CHECKING:
    from .simulation import MROEntities


def seed_spare_parts(
    engine: Engine,
    backend: SimPyBackend,
    *,
    quantity: float,
) -> None:
    if quantity <= 0 or quantity > PART_CAPACITY:
        raise ValueError("quantity must fit spare-parts capacity")

    if not any(item.item_id == "part-lot-1" for item in engine.persistence.store_items()):
        engine.stores.put(
            backend,
            store_name="spare_part_lots",
            item_id="part-lot-1",
            value={"sku": PART_SKU, "quantity": quantity},
            requested_at=backend.now,
        )

    if not any(
        intent.request_id == "seed-spare-parts"
        for intent in engine.persistence.container_operation_intents()
    ) and not any(
        result.request_id == "seed-spare-parts"
        for result in engine.persistence.container_operation_results()
    ):
        engine.containers.put(
            backend,
            container_name="spare_parts",
            request_id="seed-spare-parts",
            amount=quantity,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)


def _part_issue_complete(persistence: MemoryPersistence) -> bool:
    lot_done = any(
        result.request_id == "consume-part-lot-1"
        for result in persistence.store_get_results()
    )
    qty_done = any(
        result.request_id == "consume-spare-part-1"
        for result in persistence.container_operation_results()
    )
    return lot_done and qty_done


def reconcile_material_availability(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    quantity: float,
) -> bool:
    """Expose spare-part shortage before acquiring constrained maintenance capacity."""

    if _part_issue_complete(persistence):
        return True

    lot_available, available = _available_spare_parts(persistence, engine)
    wo = persistence.entity("work_order", entities.work_order_id)
    demand = persistence.entity("part_demand", entities.part_demand_id)
    if wo is None or demand is None:
        raise RuntimeError("MRO entities were not persisted")

    if not _material_is_available(lot_available, available, quantity):
        _reconcile_material_shortage(
            persistence,
            engine,
            backend,
            entities=entities,
            wo=wo,
            demand=demand,
        )
        return False

    if wo.state == "waiting_material":
        engine.dispatch(
            engine.context.commands.create(
                "material_ready",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "material-ready"),
            )
        )
    return True


def _can_issue_part_now(
    persistence: MemoryPersistence,
    *,
    quantity: float,
) -> bool:
    lot_available = any(item.item_id == "part-lot-1" for item in persistence.store_items())
    available = next(
        (s.level for s in persistence.container_states() if s.name == "spare_parts"),
        0.0,
    )
    return lot_available and available >= quantity


def _dispatch_material_wait(engine: Engine, *, wo, demand) -> None:
    if wo.state == "released":
        engine.dispatch(
            engine.context.commands.create(
                "wait_for_material",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "wait-material"),
            )
        )
    if demand.state == "open":
        engine.dispatch(
            engine.context.commands.create(
                "wait",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "wait"),
            )
        )


def _ensure_part_issue_requests(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    quantity: float,
) -> None:
    lot_request = "consume-part-lot-1"
    qty_request = "consume-spare-part-1"

    lot_pending = any(r.request_id == lot_request for r in persistence.store_get_requests())
    lot_done = any(r.request_id == lot_request for r in persistence.store_get_results())
    if not lot_pending and not lot_done:
        engine.stores.get(
            backend,
            store_name="spare_part_lots",
            request_id=lot_request,
            requested_at=backend.now,
        )

    qty_pending = any(
        i.request_id == qty_request
        for i in persistence.container_operation_intents()
    )
    qty_done = any(
        r.request_id == qty_request
        for r in persistence.container_operation_results()
    )
    if not qty_pending and not qty_done:
        engine.containers.get(
            backend,
            container_name="spare_parts",
            request_id=qty_request,
            amount=quantity,
            requested_at=backend.now,
        )


def _dispatch_part_demand_progress(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: MROEntities,
) -> None:
    demand = persistence.entity("part_demand", entities.part_demand_id)
    if demand is not None and demand.state in {"open", "waiting_inventory"}:
        engine.dispatch(
            engine.context.commands.create(
                "allocate",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "allocate"),
            )
        )
        demand = persistence.entity("part_demand", entities.part_demand_id)
    if demand is not None and demand.state == "allocated":
        engine.dispatch(
            engine.context.commands.create(
                "consume",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "consume"),
            )
        )


def reconcile_part_issue(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    quantity: float,
) -> bool:
    wo = persistence.entity("work_order", entities.work_order_id)
    demand = persistence.entity("part_demand", entities.part_demand_id)
    if wo is None or demand is None:
        raise RuntimeError("MRO entities were not persisted")

    already_issued = _part_issue_complete(persistence)
    if not already_issued and not _can_issue_part_now(persistence, quantity=quantity):
        _dispatch_material_wait(engine, wo=wo, demand=demand)
        return False

    if not already_issued:
        _ensure_part_issue_requests(
            persistence,
            engine,
            backend,
            quantity=quantity,
        )
    backend.run_until(backend.now)

    if not _part_issue_now_complete(persistence, already_issued=already_issued):
        return False

    _dispatch_part_demand_progress(
        persistence,
        engine,
        entities=entities,
    )
    return True


def _available_spare_parts(
    persistence: MemoryPersistence,
    engine: Engine,
) -> tuple[bool, float]:
    scenario_available = engine.context.scenarios.attribute(
        "mro.spare_parts.available", True
    )
    if not scenario_available:
        return False, 0.0
    lot_available = any(item.item_id == "part-lot-1" for item in persistence.store_items())
    available = next(
        (s.level for s in persistence.container_states() if s.name == "spare_parts"),
        0.0,
    )
    return lot_available, available


def _material_is_available(lot_available: bool, available: float, quantity: float) -> bool:
    return lot_available and available >= quantity


def _reconcile_material_shortage(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    wo,
    demand,
) -> None:
    if wo.state == "waiting_resource":
        release_capacity(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        wo = persistence.entity("work_order", entities.work_order_id)
    if wo is not None and wo.state in {"released", "waiting_resource"}:
        engine.dispatch(
            engine.context.commands.create(
                "wait_for_material",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "wait-material"),
            )
        )
    if demand.state == "open":
        engine.dispatch(
            engine.context.commands.create(
                "wait",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "wait"),
            )
        )


def _part_issue_now_complete(
    persistence: MemoryPersistence,
    *,
    already_issued: bool,
) -> bool:
    if already_issued:
        return True
    lot_done = any(
        r.request_id == "consume-part-lot-1"
        for r in persistence.store_get_results()
    )
    qty_done = any(
        r.request_id == "consume-spare-part-1"
        for r in persistence.container_operation_results()
    )
    return lot_done and qty_done


def _part_issue_started(persistence: MemoryPersistence) -> bool:
    return (
        any(
            request.request_id == "consume-part-lot-1"
            for request in persistence.store_get_requests()
        )
        or any(
            result.request_id == "consume-part-lot-1"
            for result in persistence.store_get_results()
        )
        or any(
            intent.request_id == "consume-spare-part-1"
            for intent in persistence.container_operation_intents()
        )
        or any(
            result.request_id == "consume-spare-part-1"
            for result in persistence.container_operation_results()
        )
    )
