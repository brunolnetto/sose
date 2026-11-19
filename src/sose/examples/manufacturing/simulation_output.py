"""Manufacturing WIP, quality release, rework and finished output (split from simulation.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .simulation_runtime import (
    SKU,
    _validate_quantity,
    flow_correlation_id,
)

if TYPE_CHECKING:
    from .simulation import ManufacturingEntities


def reconcile_wip_output(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
    quantity: float,
) -> float:
    """Commit produced WIP and enter inspection without claiming finished goods."""

    _validate_quantity(quantity)
    yield_factor = float(
        engine.context.scenarios.attribute("manufacturing.yield.factor", 1.0)
    )
    if yield_factor <= 0 or yield_factor > 1:
        raise ValueError("manufacturing yield factor must be in (0, 1]")
    output_quantity = quantity * yield_factor
    wip_id = "wip-1"
    _ensure_wip_store_record(
        persistence,
        engine,
        backend,
        wip_id=wip_id,
        output_quantity=output_quantity,
    )
    backend.run_until(backend.now)

    if not _wip_durable(persistence, wip_id):
        raise RuntimeError("production WIP is not durably committed")

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    if order.state == "producing":
        command = engine.context.commands.create(
            "begin_inspection",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "inspect"),
        )
        engine.dispatch(command)

    if operation.state == "running":
        command = engine.context.commands.create(
            "finish",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", operation.id, "finish"),
        )
        engine.dispatch(command)

    return output_quantity


def _ensure_wip_store_record(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    wip_id: str,
    output_quantity: float,
) -> None:
    if _wip_durable_or_pending(persistence, wip_id):
        return
    engine.stores.put(
        backend,
        store_name="wip_buffer",
        item_id=wip_id,
        value={"sku": SKU, "quantity": output_quantity},
        requested_at=backend.now,
    )


def _wip_durable_or_pending(
    persistence: MemoryPersistence,
    wip_id: str,
) -> bool:
    return any(i.item_id == wip_id for i in persistence.store_items()) or any(
        i.item_id == wip_id for i in persistence.store_put_intents()
    ) or any(
        result.item.item_id == wip_id for result in persistence.store_get_results()
    )


def _wip_durable(
    persistence: MemoryPersistence,
    wip_id: str,
) -> bool:
    return any(i.item_id == wip_id for i in persistence.store_items()) or any(
        result.item.item_id == wip_id for result in persistence.store_get_results()
    )


def _wip_for_quality_release(persistence: MemoryPersistence):
    wip = next((item for item in persistence.store_items() if item.item_id == "wip-1"), None)
    consumed = next(
        (
            result
            for result in persistence.store_get_results()
            if result.request_id == "consume-wip-1"
        ),
        None,
    )
    return wip, consumed


def _ensure_quality_release_requests(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    output_quantity: float,
    wip_exists: bool,
) -> None:
    fg_request = "finish-goods-1"
    consume_wip = "consume-wip-1"
    if not any(
        i.request_id == fg_request for i in persistence.container_operation_intents()
    ) and not any(
        r.request_id == fg_request for r in persistence.container_operation_results()
    ):
        engine.containers.put(
            backend,
            container_name="finished_goods",
            request_id=fg_request,
            amount=output_quantity,
            requested_at=backend.now,
        )

    if wip_exists and not any(
        r.request_id == consume_wip for r in persistence.store_get_requests()
    ) and not any(
        r.request_id == consume_wip for r in persistence.store_get_results()
    ):
        engine.stores.get(
            backend,
            store_name="wip_buffer",
            request_id=consume_wip,
            requested_at=backend.now,
        )


def _quality_release_committed(persistence: MemoryPersistence) -> bool:
    fg_done = any(
        r.request_id == "finish-goods-1"
        for r in persistence.container_operation_results()
    )
    wip_done = any(
        r.request_id == "consume-wip-1"
        for r in persistence.store_get_results()
    )
    return fg_done and wip_done


def reconcile_quality_pass(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
    quantity: float,
) -> None:
    """Release inspected WIP to finished goods, then complete the order."""

    _validate_quantity(quantity)
    order = persistence.entity("production_order", entities.production_order_id)
    if order is None:
        raise RuntimeError("production order was not persisted")
    if order.state != "inspection" and order.state != "completed":
        raise RuntimeError(f"production order is not ready for quality release: {order.state}")

    wip, consumed = _wip_for_quality_release(persistence)
    if wip is None and consumed is None:
        raise RuntimeError("quality release requires durable WIP")

    output_quantity = float(
        (wip.value if wip is not None else consumed.item.value)["quantity"]
    )
    _ensure_quality_release_requests(
        persistence,
        engine,
        backend,
        output_quantity=output_quantity,
        wip_exists=wip is not None,
    )
    backend.run_until(backend.now)

    if not _quality_release_committed(persistence):
        raise RuntimeError("quality release is not durably committed")

    order = persistence.entity("production_order", entities.production_order_id)
    if order is not None and order.state == "inspection":
        command = engine.context.commands.create(
            "complete",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "complete"),
        )
        engine.dispatch(command)


def reconcile_quality_hold(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ManufacturingEntities,
) -> None:
    """Put inspected WIP on durable quality hold without creating finished goods."""

    order = persistence.entity("production_order", entities.production_order_id)
    if order is None:
        raise RuntimeError("production order was not persisted")
    if order.state == "inspection":
        command = engine.context.commands.create(
            "hold_quality",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-quality", order.id, "hold"),
        )
        engine.dispatch(command)
    elif order.state != "quality_hold":
        raise RuntimeError(f"production order is not inspectable: {order.state}")


def reconcile_rework(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ManufacturingEntities,
) -> None:
    """Return held WIP to production without issuing raw material a second time."""

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    if order.state == "quality_hold":
        command = engine.context.commands.create(
            "rework_order",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-quality", order.id, "rework"),
        )
        engine.dispatch(command)
        order = persistence.entity("production_order", entities.production_order_id)

    if order is not None and order.state == "rework":
        command = engine.context.commands.create(
            "resume_rework",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-quality", order.id, "resume"),
        )
        engine.dispatch(command)

    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if operation is not None and operation.state == "done":
        command = engine.context.commands.create(
            "rework",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-quality", operation.id, "rework"),
        )
        engine.dispatch(command)
        operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if operation is not None and operation.state == "ready_state":
        command = engine.context.commands.create(
            "start",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-quality", operation.id, "restart"),
        )
        engine.dispatch(command)


def reconcile_output(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
    quantity: float,
) -> None:
    """Happy-path wrapper: produce WIP, pass inspection, release finished goods."""

    reconcile_wip_output(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=quantity,
    )
    reconcile_quality_pass(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=quantity,
    )
