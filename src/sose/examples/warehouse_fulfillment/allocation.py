"""Local-inventory order allocation for warehouse fulfillment (split from simulation.py)."""

from __future__ import annotations

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import Allocation, FulfillmentOrder, InventoryLot
from .records import WarehouseEntities, _dispatch, _lot_entity, _order_entity


def _available_quantity(lot: InventoryLot) -> float:
    return max(
        0.0,
        float(lot.attributes["on_hand"]) - float(lot.attributes["allocated"]),
    )


def _resolve_existing_allocations(
    persistence: MemoryPersistence,
    order: FulfillmentOrder,
) -> bool:
    existing_ids = list(order.attributes.get("allocation_ids", []))
    if not existing_ids:
        return False
    if all(
        persistence.entity("warehouse_allocation", str(aid)) is not None
        for aid in existing_ids
    ):
        return True
    raise RuntimeError("allocation index references missing durable allocation")


def _eligible_lots_for_order(
    persistence: MemoryPersistence,
    *,
    entities: WarehouseEntities,
    requested_sku: str,
    acceptable: tuple[str, ...],
) -> list[InventoryLot]:
    lots = [_lot_entity(persistence, lot_id) for lot_id in entities.lot_ids]
    eligible = [
        lot
        for lot in lots
        if str(lot.attributes["sku"]) in acceptable
        and _available_quantity(lot) > 0
    ]
    eligible.sort(
        key=lambda lot: (
            0 if lot.attributes["sku"] == requested_sku else 1,
            lot.id,
        )
    )
    return eligible


def _build_allocation_plan(
    eligible_lots: list[InventoryLot],
    *,
    needed: float,
) -> list[tuple[InventoryLot, float]] | None:
    plan: list[tuple[InventoryLot, float]] = []
    remaining = needed
    for lot in eligible_lots:
        quantity = min(remaining, _available_quantity(lot))
        if quantity > 0:
            plan.append((lot, quantity))
            remaining -= quantity
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    return plan


def _materialize_allocations(
    engine: Engine,
    order: FulfillmentOrder,
    plan: list[tuple[InventoryLot, float]],
    *,
    requested_sku: str,
) -> list[Allocation]:
    allocations: list[Allocation] = []
    for lot, quantity in plan:
        allocation = engine.context.entities.create(
            Allocation,
            key=("warehouse-reference", order.id, lot.id),
            state="committed",
            attributes={
                "order_id": order.id,
                "lot_id": lot.id,
                "requested_sku": requested_sku,
                "supplied_sku": lot.attributes["sku"],
                "quantity": quantity,
                "substituted": lot.attributes["sku"] != requested_sku,
            },
        )
        lot.attributes["allocated"] = (
            float(lot.attributes["allocated"]) + quantity
        )
        allocations.append(allocation)
    return allocations


def allocate_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
) -> bool:
    order = _order_entity(persistence, entities)
    if order.state in {"allocated", "picking", "packed", "shipped"}:
        return True
    if order.state != "requested":
        return False

    if _resolve_existing_allocations(persistence, order):
        _dispatch(
            engine,
            order,
            "allocate",
            key=("warehouse-order", order.id, "allocate"),
        )
        return True

    requested_sku = str(order.attributes["requested_sku"])
    acceptable = tuple(str(v) for v in order.attributes["acceptable_skus"])
    needed = float(order.attributes["requested_quantity"])
    eligible = _eligible_lots_for_order(
        persistence,
        entities=entities,
        requested_sku=requested_sku,
        acceptable=acceptable,
    )
    plan = _build_allocation_plan(eligible, needed=needed)
    if plan is None:
        return False

    allocations = _materialize_allocations(
        engine,
        order,
        plan,
        requested_sku=requested_sku,
    )

    order.attributes["allocation_ids"] = [
        allocation.id for allocation in allocations
    ]
    with persistence.transaction() as uow:
        uow.save_entity(order)
        for lot, _ in plan:
            uow.save_entity(lot)
        for allocation in allocations:
            uow.save_entity(allocation)

    order = _order_entity(persistence, entities)
    _dispatch(
        engine,
        order,
        "allocate",
        key=("warehouse-order", order.id, "allocate"),
    )
    return True
