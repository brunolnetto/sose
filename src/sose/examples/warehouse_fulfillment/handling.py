"""Local pick/pack/ship occurrences and lot corrections (split from simulation.py)."""

from __future__ import annotations

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import Allocation, InventoryLot, InventoryOccurrence
from .records import (
    WarehouseEntities,
    _allocation_entity,
    _dispatch,
    _entity,
    _lot_entity,
    _order_entity,
    occurrence_id,
)


def _ensure_occurrence(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    kind: str,
    subject_id: str,
    sequence: int,
    attributes: dict[str, object],
    owner=None,
    lot: InventoryLot | None = None,
) -> InventoryOccurrence:
    oid = occurrence_id(kind, subject_id, sequence)
    existing = persistence.entity("warehouse_inventory_occurrence", oid)
    if existing is not None:
        _validate_occurrence_identity(existing, attributes)
    else:
        occurrence = engine.context.entities.create(
            InventoryOccurrence,
            key=("warehouse-reference", kind, subject_id, sequence),
            state="captured",
            attributes={
                "kind": kind,
                "subject_id": subject_id,
                "sequence": sequence,
                **attributes,
            },
        )
        _append_occurrence_id(owner, occurrence.id)
        _append_occurrence_id(lot, occurrence.id)
        _persist_occurrence_scope(
            persistence,
            occurrence=occurrence,
            owner=owner,
            lot=lot,
        )

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
    return _entity(
        persistence,
        "warehouse_inventory_occurrence",
        oid,
    )


def _validate_occurrence_identity(
    occurrence: InventoryOccurrence,
    attributes: dict[str, object],
) -> None:
    for key, value in attributes.items():
        if occurrence.attributes.get(key) != value:
            raise ValueError(
                "occurrence identity already exists with different evidence"
            )


def _append_occurrence_id(entity, occurrence_id_value: str) -> None:
    if entity is None:
        return
    occurrence_ids = list(entity.attributes.get("occurrence_ids", []))
    if occurrence_id_value in occurrence_ids:
        return
    occurrence_ids.append(occurrence_id_value)
    entity.attributes["occurrence_ids"] = occurrence_ids


def _persist_occurrence_scope(
    persistence: MemoryPersistence,
    *,
    occurrence: InventoryOccurrence,
    owner,
    lot: InventoryLot | None,
) -> None:
    with persistence.transaction() as uow:
        uow.save_entity(occurrence)
        if owner is not None:
            uow.save_entity(owner)
        if lot is not None:
            uow.save_entity(lot)


def pick_allocation(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
    allocation_id_value: str,
) -> Allocation:
    order = _order_entity(persistence, entities)
    allocation = _allocation_entity(persistence, allocation_id_value)
    if order.state == "allocated":
        _dispatch(
            engine,
            order,
            "start_pick",
            key=("warehouse-order", order.id, "start-pick"),
        )
        order = _order_entity(persistence, entities)
    if order.state != "picking":
        raise RuntimeError("picking requires allocated fulfillment order")
    if allocation.state in {"picked", "shipped"}:
        return allocation

    lot = _lot_entity(persistence, str(allocation.attributes["lot_id"]))
    quantity = float(allocation.attributes["quantity"])
    oid = occurrence_id("pick", allocation.id, 1)
    occurrence = persistence.entity("warehouse_inventory_occurrence", oid)

    if occurrence is None:
        if float(lot.attributes["allocated"]) + 1e-9 < quantity:
            raise RuntimeError("lot allocation projection is below committed quantity")
        if float(lot.attributes["on_hand"]) + 1e-9 < quantity:
            raise RuntimeError("lot on-hand projection is below committed quantity")
        lot.attributes["on_hand"] = round(
            float(lot.attributes["on_hand"]) - quantity,
            6,
        )
        lot.attributes["allocated"] = round(
            float(lot.attributes["allocated"]) - quantity,
            6,
        )
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
                "lot_id": lot.id,
                "sku": allocation.attributes["supplied_sku"],
                "quantity": quantity,
                "delta_on_hand": -quantity,
                "resulting_on_hand": lot.attributes["on_hand"],
            },
        )
        order_occurrences = list(order.attributes.get("occurrence_ids", []))
        order_occurrences.append(occurrence.id)
        order.attributes["occurrence_ids"] = order_occurrences
        lot_occurrences = list(lot.attributes.get("occurrence_ids", []))
        lot_occurrences.append(occurrence.id)
        lot.attributes["occurrence_ids"] = lot_occurrences
        with persistence.transaction() as uow:
            uow.save_entity(lot)
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
    return _allocation_entity(persistence, allocation.id)

def pick_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
) -> bool:
    order = _order_entity(persistence, entities)
    if order.state == "shipped":
        return True
    if order.state not in {"allocated", "picking"}:
        raise RuntimeError("pick requires allocated fulfillment order")
    for allocation_id_value in order.attributes.get("allocation_ids", []):
        pick_allocation(
            persistence,
            engine,
            entities=entities,
            allocation_id_value=str(allocation_id_value),
        )
    return True


def pack_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
) -> bool:
    order = _order_entity(persistence, entities)
    if order.state == "packed":
        return True
    if order.state != "picking":
        raise RuntimeError("pack requires picking fulfillment order")
    allocations = [
        _allocation_entity(persistence, str(aid))
        for aid in order.attributes.get("allocation_ids", [])
    ]
    if not allocations or any(a.state != "picked" for a in allocations):
        raise RuntimeError("pack requires all allocations to be picked")

    _ensure_occurrence(
        persistence,
        engine,
        kind="pack",
        subject_id=order.id,
        sequence=1,
        attributes={
            "order_id": order.id,
            "allocation_ids": [a.id for a in allocations],
        },
        owner=order,
    )
    order = _order_entity(persistence, entities)
    _dispatch(
        engine,
        order,
        "pack",
        key=("warehouse-order", order.id, "pack"),
    )
    return True


def ship_order(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: WarehouseEntities,
) -> bool:
    order = _order_entity(persistence, entities)
    if order.state == "shipped":
        return True
    if order.state != "packed":
        raise RuntimeError("ship requires packed fulfillment order")
    allocations = [
        _allocation_entity(persistence, str(aid))
        for aid in order.attributes.get("allocation_ids", [])
    ]
    for allocation in allocations:
        if allocation.state == "picked":
            _dispatch(
                engine,
                allocation,
                "ship",
                key=("warehouse-allocation", allocation.id, "ship"),
            )

    _ensure_occurrence(
        persistence,
        engine,
        kind="ship",
        subject_id=order.id,
        sequence=1,
        attributes={
            "order_id": order.id,
            "allocation_ids": [a.id for a in allocations],
        },
        owner=_order_entity(persistence, entities),
    )
    order = _order_entity(persistence, entities)
    _dispatch(
        engine,
        order,
        "ship",
        key=("warehouse-order", order.id, "ship"),
    )
    return True

def correct_lot_balance(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    lot_id: str,
    sequence: int,
    delta: float,
) -> InventoryOccurrence:
    if sequence <= 0:
        raise ValueError("correction sequence must be positive")
    oid = occurrence_id("correction", lot_id, sequence)
    existing = persistence.entity("warehouse_inventory_occurrence", oid)
    if existing is not None:
        if float(existing.attributes["delta_on_hand"]) != float(delta):
            raise ValueError(
                "occurrence identity already exists with different evidence"
            )
        if existing.state == "captured":
            _dispatch(
                engine,
                existing,
                "commit",
                key=("warehouse-occurrence", existing.id, "commit"),
            )
        return _entity(
            persistence,
            "warehouse_inventory_occurrence",
            oid,
        )

    lot = _lot_entity(persistence, lot_id)
    next_on_hand = round(float(lot.attributes["on_hand"]) + float(delta), 6)
    if next_on_hand < float(lot.attributes["allocated"]) - 1e-9:
        raise ValueError("correction cannot reduce on-hand below allocated quantity")
    if next_on_hand < -1e-9:
        raise ValueError("correction cannot make on-hand negative")

    lot.attributes["on_hand"] = next_on_hand
    occurrence = engine.context.entities.create(
        InventoryOccurrence,
        key=("warehouse-reference", "correction", lot.id, sequence),
        state="captured",
        attributes={
            "kind": "correction",
            "subject_id": lot.id,
            "sequence": sequence,
            "lot_id": lot.id,
            "sku": lot.attributes["sku"],
            "delta_on_hand": float(delta),
            "resulting_on_hand": next_on_hand,
        },
    )
    occurrence_ids = list(lot.attributes.get("occurrence_ids", []))
    occurrence_ids.append(occurrence.id)
    lot.attributes["occurrence_ids"] = occurrence_ids
    with persistence.transaction() as uow:
        uow.save_entity(lot)
        uow.save_entity(occurrence)

    _dispatch(
        engine,
        occurrence,
        "commit",
        key=("warehouse-occurrence", occurrence.id, "commit"),
    )
    return _entity(
        persistence,
        "warehouse_inventory_occurrence",
        occurrence.id,
    )
