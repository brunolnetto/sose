"""Composed-mode fulfillment against WM-owned stock (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime

from sose.core.engine import Engine
from sose.core.runtime import ResourceDefinition
from sose.persistence.memory import MemoryPersistence

from .entities import Allocation, FulfillmentOrder, InventoryOccurrence
from .records import (
    PACKING_RESOURCE,
    PICKER_RESOURCE,
    PRIMARY_SKU,
    SHIPPING_RESOURCE,
    WarehouseEntities,
    _allocation_entity,
    _dispatch,
    _entity,
    _order_entity,
    build_runtime,
    occurrence_id,
)
from .scenarios import ORIGIN


def seed_composed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    requested_quantity: float = 10.0,
    requested_sku: str = PRIMARY_SKU,
    request_key: str = "order-1",
    picker_capacity: int = 1,
    packing_station_capacity: int = 1,
    shipping_dock_capacity: int = 1,
) -> WarehouseEntities:
    """Seed fulfillment without local authoritative inventory.

    In composed mode Warehouse Management owns stock truth. Fulfillment retains
    only the order, resources, allocations, occurrences, and immutable foreign
    stock/reservation references.
    """

    if requested_quantity <= 0:
        raise ValueError("requested_quantity must be positive")
    if not requested_sku:
        raise ValueError("requested_sku cannot be empty")
    if not request_key:
        raise ValueError("composed request_key must be nonempty")
    for value, name in (
        (picker_capacity, "picker_capacity"),
        (packing_station_capacity, "packing_station_capacity"),
        (shipping_dock_capacity, "shipping_dock_capacity"),
    ):
        if value < 1:
            raise ValueError(f"{name} must be at least one")

    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
    order = context.entities.create(
        FulfillmentOrder,
        key=("warehouse-reference", request_key),
        state="requested",
        attributes={
            "requested_sku": requested_sku,
            "acceptable_skus": [requested_sku],
            "requested_quantity": float(requested_quantity),
            "allocation_ids": [],
            "occurrence_ids": [],
            "inventory_owner": "warehouse_management",
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(order)
        uow.save_resource_definition(
            ResourceDefinition(PICKER_RESOURCE, capacity=picker_capacity)
        )
        uow.save_resource_definition(
            ResourceDefinition(PACKING_RESOURCE, capacity=packing_station_capacity)
        )
        uow.save_resource_definition(
            ResourceDefinition(SHIPPING_RESOURCE, capacity=shipping_dock_capacity)
        )
    return WarehouseEntities(order_id=order.id, lot_ids=())


def _validate_composed_allocation_request(
    *,
    stock_reference: str,
    reservation_reference: str,
    supplied_sku: str,
    quantity: float,
) -> None:
    if not stock_reference:
        raise ValueError("stock_reference cannot be empty")
    if not reservation_reference:
        raise ValueError("reservation_reference cannot be empty")
    if not supplied_sku:
        raise ValueError("supplied_sku cannot be empty")
    if quantity <= 0:
        raise ValueError("quantity must be positive")


def _validate_composed_order_reservation(
    order: FulfillmentOrder,
    *,
    supplied_sku: str,
    quantity: float,
) -> None:
    if order.attributes.get("inventory_owner") != "warehouse_management":
        raise ValueError("composed allocation requires WM-owned inventory")
    requested = float(order.attributes["requested_quantity"])
    if abs(requested - float(quantity)) > 1e-9:
        raise ValueError("composed reservation quantity must satisfy the order")
    acceptable = tuple(str(value) for value in order.attributes["acceptable_skus"])
    if supplied_sku not in acceptable:
        raise ValueError("composed reservation SKU is not acceptable")


def _replay_composed_allocation(
    persistence: MemoryPersistence,
    engine: Engine,
    order: FulfillmentOrder,
    existing_ids: list,
    *,
    stock_reference: str,
    reservation_reference: str,
    supplied_sku: str,
    quantity: float,
) -> None:
    if len(existing_ids) != 1:
        raise RuntimeError("composed order must have one reservation allocation")
    allocation = _allocation_entity(persistence, str(existing_ids[0]))
    expected = {
        "inventory_owner": "warehouse_management",
        "stock_reference": stock_reference,
        "reservation_reference": reservation_reference,
        "supplied_sku": supplied_sku,
        "quantity": float(quantity),
    }
    if any(allocation.attributes.get(key) != value for key, value in expected.items()):
        raise ValueError("composed allocation replay conflict")
    if order.state == "requested":
        _dispatch(
            engine,
            order,
            "allocate",
            key=("warehouse-order", order.id, "allocate"),
        )


def allocate_composed_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
    stock_reference: str,
    reservation_reference: str,
    supplied_sku: str,
    quantity: float,
) -> bool:
    """Allocate from a WM-owned reservation without creating local stock truth."""

    _validate_composed_allocation_request(
        stock_reference=stock_reference,
        reservation_reference=reservation_reference,
        supplied_sku=supplied_sku,
        quantity=quantity,
    )

    order = _order_entity(persistence, entities)
    _validate_composed_order_reservation(
        order,
        supplied_sku=supplied_sku,
        quantity=quantity,
    )

    existing_ids = list(order.attributes.get("allocation_ids", []))
    if existing_ids:
        _replay_composed_allocation(
            persistence,
            engine,
            order,
            existing_ids,
            stock_reference=stock_reference,
            reservation_reference=reservation_reference,
            supplied_sku=supplied_sku,
            quantity=quantity,
        )
        return True

    if order.state != "requested":
        return order.state in {"allocated", "picking", "packed", "shipped"}

    requested_sku = str(order.attributes["requested_sku"])
    allocation = engine.context.entities.create(
        Allocation,
        key=("warehouse-composed", order.id, reservation_reference),
        state="committed",
        attributes={
            "order_id": order.id,
            "requested_sku": requested_sku,
            "supplied_sku": supplied_sku,
            "quantity": float(quantity),
            "substituted": supplied_sku != requested_sku,
            "inventory_owner": "warehouse_management",
            "stock_reference": stock_reference,
            "reservation_reference": reservation_reference,
        },
    )
    order.attributes["allocation_ids"] = [allocation.id]
    with persistence.transaction() as uow:
        uow.save_entity(order)
        uow.save_entity(allocation)

    order = _order_entity(persistence, entities)
    _dispatch(
        engine,
        order,
        "allocate",
        key=("warehouse-order", order.id, "allocate"),
    )
    return True


def pick_composed_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
) -> bool:
    """Record fulfillment picking against foreign WM reservation references."""

    order = _order_entity(persistence, entities)
    if order.state == "shipped":
        return True
    if order.state == "allocated":
        _dispatch(
            engine,
            order,
            "start_pick",
            key=("warehouse-order", order.id, "start-pick"),
        )
        order = _order_entity(persistence, entities)
    if order.state != "picking":
        raise RuntimeError("composed pick requires allocated fulfillment order")

    allocation_ids = tuple(str(value) for value in order.attributes.get("allocation_ids", []))
    if not allocation_ids:
        raise RuntimeError("composed pick requires a foreign reservation allocation")

    for allocation_id_value in allocation_ids:
        _pick_composed_allocation(persistence, engine, order, allocation_id_value)

    return True


def _pick_composed_allocation(
    persistence: MemoryPersistence,
    engine: Engine,
    order: FulfillmentOrder,
    allocation_id_value: str,
) -> None:
    allocation = _allocation_entity(persistence, allocation_id_value)
    if allocation.attributes.get("inventory_owner") != "warehouse_management":
        raise ValueError("composed pick cannot consume local inventory")
    if "lot_id" in allocation.attributes:
        raise ValueError("composed allocation cannot contain local lot ownership")
    if allocation.state in {"picked", "shipped"}:
        return
    if allocation.state != "committed":
        raise RuntimeError("composed allocation is not ready to pick")

    quantity = float(allocation.attributes["quantity"])
    oid = occurrence_id("pick", allocation.id, 1)
    occurrence = persistence.entity("warehouse_inventory_occurrence", oid)
    if occurrence is None:
        occurrence = engine.context.entities.create(
            InventoryOccurrence,
            key=("warehouse-reference", "pick", allocation.id, 1),
            state="captured",
            attributes={
                "kind": "pick",
                "subject_id": allocation.id,
                "sequence": 1,
                "order_id": order.id,
                "allocation_id": allocation.id,
                "inventory_owner": "warehouse_management",
                "stock_reference": allocation.attributes["stock_reference"],
                "reservation_reference": allocation.attributes[
                    "reservation_reference"
                ],
                "sku": allocation.attributes["supplied_sku"],
                "quantity": quantity,
            },
        )
        occurrence_ids = list(order.attributes.get("occurrence_ids", []))
        if occurrence.id not in occurrence_ids:
            occurrence_ids.append(occurrence.id)
            order.attributes["occurrence_ids"] = occurrence_ids
        with persistence.transaction() as uow:
            uow.save_entity(order)
            uow.save_entity(occurrence)

    occurrence = _entity(
        persistence,
        "warehouse_inventory_occurrence",
        oid,
    )
    if occurrence.state == "captured":
        _dispatch(
            engine,
            occurrence,
            "commit",
            key=("warehouse-occurrence", occurrence.id, "commit"),
        )

    allocation = _allocation_entity(persistence, allocation.id)
    if allocation.state == "committed":
        _dispatch(
            engine,
            allocation,
            "pick",
            key=("warehouse-allocation", allocation.id, "pick"),
        )
