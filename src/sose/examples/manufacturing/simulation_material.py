"""Manufacturing raw-material stock and material issue (split from simulation.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .simulation_runtime import (
    RAW_SKU,
    _validate_quantity,
    flow_correlation_id,
)

if TYPE_CHECKING:
    from .simulation import ManufacturingEntities


def seed_material(
    engine: Engine,
    backend: SimPyBackend,
    *,
    quantity: float,
) -> None:
    _validate_quantity(quantity)
    if not any(i.item_id == "raw-lot-1" for i in engine.persistence.store_items()):
        engine.stores.put(
            backend,
            store_name="raw_material_lots",
            item_id="raw-lot-1",
            value={"sku": RAW_SKU, "quantity": quantity},
            requested_at=backend.now,
        )
    if not any(
        i.request_id == "seed-raw-material"
        for i in engine.persistence.container_operation_intents()
    ) and not any(
        r.request_id == "seed-raw-material"
        for r in engine.persistence.container_operation_results()
    ):
        engine.containers.put(
            backend,
            container_name="raw_material",
            request_id="seed-raw-material",
            amount=quantity,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)


def reconcile_material_availability(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ManufacturingEntities,
) -> None:
    """Keep shortage visible in durable business state until replenishment exists."""

    order = persistence.entity("production_order", entities.production_order_id)
    if order is None:
        raise RuntimeError("production order was not persisted")

    has_lot = bool(persistence.store_items())
    raw_level = next(
        (state.level for state in persistence.container_states() if state.name == "raw_material"),
        0.0,
    )
    if order.state == "released" and (not has_lot or raw_level <= 0):
        command = engine.context.commands.create(
            "wait_for_material",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "wait-material"),
        )
        engine.dispatch(command)
    elif order.state == "waiting_material" and has_lot and raw_level > 0:
        command = engine.context.commands.create(
            "material_ready",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "material-ready"),
        )
        engine.dispatch(command)


def reconcile_material_issue(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
    quantity: float,
) -> None:
    _validate_quantity(quantity)
    lot_request = "issue-raw-lot-1"
    qty_request = "issue-raw-material-1"
    _ensure_material_issue_requests(
        persistence,
        engine,
        backend,
        lot_request=lot_request,
        qty_request=qty_request,
        quantity=quantity,
    )
    backend.run_until(backend.now)

    if not _material_issue_completed(
        persistence,
        lot_request=lot_request,
        qty_request=qty_request,
    ):
        raise RuntimeError("raw material issue is still pending")

    order = persistence.entity("production_order", entities.production_order_id)
    if order is None:
        raise RuntimeError("production order was not persisted")
    if order.state == "setup":
        command = engine.context.commands.create(
            "start_production",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "start-production"),
        )
        engine.dispatch(command)


def _ensure_material_issue_requests(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    lot_request: str,
    qty_request: str,
    quantity: float,
) -> None:
    if not _has_store_get_activity(persistence, lot_request):
        engine.stores.get(
            backend,
            store_name="raw_material_lots",
            request_id=lot_request,
            requested_at=backend.now,
        )
    if _has_container_withdraw_activity(persistence, qty_request):
        return
    engine.containers.get(
        backend,
        container_name="raw_material",
        request_id=qty_request,
        amount=quantity,
        requested_at=backend.now,
    )


def _has_store_get_activity(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(r.request_id == request_id for r in persistence.store_get_requests()) or any(
        r.request_id == request_id for r in persistence.store_get_results()
    )


def _has_container_withdraw_activity(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(i.request_id == request_id for i in persistence.container_operation_intents()) or any(
        r.request_id == request_id for r in persistence.container_operation_results()
    )


def _material_issue_completed(
    persistence: MemoryPersistence,
    *,
    lot_request: str,
    qty_request: str,
) -> bool:
    lot_done = any(r.request_id == lot_request for r in persistence.store_get_results())
    qty_done = any(
        r.request_id == qty_request
        for r in persistence.container_operation_results()
    )
    return lot_done and qty_done
