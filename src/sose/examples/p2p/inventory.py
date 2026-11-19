"""Procure-to-pay stocking, shortage and consumption reconciliation (split from simulation.py)."""

from __future__ import annotations


from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .foundation import (
    P2PEntities,
    SKU,
    _validate_quantity,
    flow_correlation_id,
)


def reconcile_stocking(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: P2PEntities,
    quantity: float,
) -> None:
    """Idempotently materialize inspected receipt inventory, then mark it stocked.

    The Store/Container records are durable recovery checkpoints. If a process stops
    after either submission or completion, rebuilding the backend plus rerunning this
    reconciler resumes from those records instead of repeating an already-committed
    effect.
    """

    _validate_quantity(quantity)
    correlation_id = flow_correlation_id()
    lot_id = "receipt-lot-1"
    stock_request_id = "stock-receipt-1"

    _ensure_stocking_side_effects(
        persistence,
        engine,
        backend,
        lot_id=lot_id,
        stock_request_id=stock_request_id,
        quantity=quantity,
    )

    backend.run_until(backend.now)

    lot_durable = _lot_durable(persistence, lot_id)
    stock_done = _container_result_exists(persistence, stock_request_id)
    if not lot_durable or not stock_done:
        raise RuntimeError("receipt inventory is not durably stocked yet")

    receipt = persistence.entity("receipt", entities.receipt_id)
    if receipt is None:
        raise RuntimeError("receipt was not persisted")
    if receipt.state == "inspected":
        command = engine.context.commands.create(
            "stock",
            target=receipt,
            correlation_id=correlation_id,
            key=("p2p-happy", receipt.entity_type, receipt.id, "stock"),
        )
        engine.dispatch(command)
    elif receipt.state != "stocked":
        raise RuntimeError(f"receipt is not ready to stock: {receipt.state}")



def reconcile_shortage_state(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: P2PEntities,
    required_quantity: float,
) -> bool:
    """Expose insufficient inventory as durable demand state before withdrawal.

    This guard deliberately runs before Store/Container GET operations so a
    partial receipt cannot consume the discrete lot while the quantitative
    withdrawal remains blocked.
    """

    _validate_quantity(required_quantity)
    available = next(
        (
            state.level
            for state in persistence.container_states()
            if state.name == "inventory"
        ),
        0.0,
    )
    if available >= required_quantity:
        return False

    demand = persistence.entity("material_demand", entities.material_demand_id)
    if demand is None:
        raise RuntimeError("material demand was not persisted")

    if demand.state == "open":
        wait = engine.context.commands.create(
            "wait_for_inventory",
            target=demand,
            correlation_id=flow_correlation_id(),
            key=("p2p-shortage", demand.id, "wait"),
        )
        engine.dispatch(wait)
        demand = persistence.entity("material_demand", entities.material_demand_id)

    if demand is not None and demand.state == "waiting_inventory":
        backorder = engine.context.commands.create(
            "backorder",
            target=demand,
            correlation_id=flow_correlation_id(),
            key=("p2p-shortage", demand.id, "backorder"),
        )
        engine.dispatch(backorder)

    return True

def reconcile_consumption(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: P2PEntities,
    quantity: float,
) -> None:
    """Idempotently withdraw stocked inventory before consuming MaterialDemand."""

    _validate_quantity(quantity)
    correlation_id = flow_correlation_id()
    lot_request_id = "consume-receipt-lot-1"
    quantity_request_id = "consume-demand-1"

    _ensure_consumption_side_effects(
        persistence,
        engine,
        backend,
        lot_request_id=lot_request_id,
        quantity_request_id=quantity_request_id,
        quantity=quantity,
    )

    backend.run_until(backend.now)

    lot_done = _store_get_result_exists(persistence, lot_request_id)
    quantity_done = _container_result_exists(persistence, quantity_request_id)
    if not lot_done or not quantity_done:
        raise RuntimeError("material demand inventory withdrawal is still pending")

    demand = persistence.entity("material_demand", entities.material_demand_id)
    if demand is None:
        raise RuntimeError("material demand was not persisted")
    if demand.state in {"open", "waiting_inventory", "backordered"}:
        allocate = engine.context.commands.create(
            "allocate",
            target=demand,
            correlation_id=correlation_id,
            key=("p2p-happy", demand.entity_type, demand.id, "allocate"),
        )
        engine.dispatch(allocate)
        demand = persistence.entity("material_demand", entities.material_demand_id)

    if demand.state == "allocated":
        consume = engine.context.commands.create(
            "consume",
            target=demand,
            correlation_id=correlation_id,
            key=("p2p-happy", demand.entity_type, demand.id, "consume"),
        )
        engine.dispatch(consume)
    elif demand.state != "consumed":
        raise RuntimeError(f"material demand is not ready to consume: {demand.state}")


def _store_get_request_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        request.request_id == request_id
        for request in persistence.store_get_requests()
    )


def _store_get_result_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        result.request_id == request_id
        for result in persistence.store_get_results()
    )


def _container_intent_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        intent.request_id == request_id
        for intent in persistence.container_operation_intents()
    )


def _container_result_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        result.request_id == request_id
        for result in persistence.container_operation_results()
    )


def _lot_visible_or_pending(
    persistence: MemoryPersistence,
    lot_id: str,
) -> bool:
    return any(item.item_id == lot_id for item in persistence.store_items()) or any(
        intent.item_id == lot_id for intent in persistence.store_put_intents()
    ) or any(
        result.item.item_id == lot_id for result in persistence.store_get_results()
    )


def _lot_durable(
    persistence: MemoryPersistence,
    lot_id: str,
) -> bool:
    return any(item.item_id == lot_id for item in persistence.store_items()) or any(
        result.item.item_id == lot_id for result in persistence.store_get_results()
    )


def _ensure_stocking_side_effects(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    lot_id: str,
    stock_request_id: str,
    quantity: float,
) -> None:
    if not _lot_visible_or_pending(persistence, lot_id):
        engine.stores.put(
            backend,
            store_name="received_lots",
            item_id=lot_id,
            value={"sku": SKU, "quantity": quantity},
            requested_at=backend.now,
        )
    if _container_result_exists(persistence, stock_request_id) or _container_intent_exists(
        persistence,
        stock_request_id,
    ):
        return
    engine.containers.put(
        backend,
        container_name="inventory",
        request_id=stock_request_id,
        amount=quantity,
        requested_at=backend.now,
    )


def _ensure_consumption_side_effects(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    lot_request_id: str,
    quantity_request_id: str,
    quantity: float,
) -> None:
    if not _store_get_request_exists(
        persistence,
        lot_request_id,
    ) and not _store_get_result_exists(
        persistence,
        lot_request_id,
    ):
        engine.stores.get(
            backend,
            store_name="received_lots",
            request_id=lot_request_id,
            requested_at=backend.now,
        )
    if _container_intent_exists(persistence, quantity_request_id) or _container_result_exists(
        persistence,
        quantity_request_id,
    ):
        return
    engine.containers.get(
        backend,
        container_name="inventory",
        request_id=quantity_request_id,
        amount=quantity,
        requested_at=backend.now,
    )
