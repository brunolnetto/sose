"""Foreign-process stock receipts, reservations and consumption (split from simulation.py)."""

from __future__ import annotations

from sose.core.engine import Engine
from sose.persistence.base import Persistence
from sose.examples.warehouse_management.reservations import (
    matching_reservation,
    validated_consumption_request,
)

from .records import _entity


def receive_external_replenishment(
    persistence: Persistence,
    engine: Engine,
    *,
    stock_id: str,
    quantity: float,
    sku: str,
    receipt_reference: str,
    caused_by=None,
    correlation_id: str | None = None,
) -> bool:
    """Idempotently apply a foreign procurement receipt to WM-owned stock truth."""

    if quantity <= 0:
        raise ValueError("replenishment quantity must be positive")
    if not sku:
        raise ValueError("replenishment sku cannot be empty")
    if not receipt_reference:
        raise ValueError("receipt_reference cannot be empty")

    stock = _entity(persistence, "warehouse_management_stock", stock_id)
    stock_sku = str(stock.attributes.get("sku", ""))
    if stock_sku != sku:
        raise ValueError(
            f"replenishment SKU mismatch: stock={stock_sku!r}, receipt={sku!r}"
        )
    receipts = dict(stock.attributes.get("external_receipts", {}))
    existing = receipts.get(receipt_reference)
    if existing is not None:
        if float(existing) != float(quantity):
            raise ValueError(
                f"receipt replay conflict: {receipt_reference}"
            )
        return False

    stock.attributes["on_hand"] = float(stock.attributes["on_hand"]) + float(quantity)
    receipts[receipt_reference] = float(quantity)
    stock.attributes["external_receipts"] = receipts

    event = engine.context.events.create(
        "warehouse_management.replenishment_received",
        entity=stock,
        caused_by=caused_by,
        correlation_id=correlation_id,
        key=("warehouse-management", stock.id, "external-receipt", receipt_reference),
        receipt_reference=receipt_reference,
        sku=sku,
        quantity=float(quantity),
    )
    with persistence.transaction() as uow:
        uow.save_entity(stock)
        uow.append_event(event)
    return True

def reserve_external_stock(
    persistence: Persistence,
    engine: Engine,
    *,
    stock_id: str,
    quantity: float,
    sku: str,
    reservation_reference: str,
    caused_by=None,
    correlation_id: str | None = None,
) -> bool:
    """Idempotently reserve WM-owned stock for a foreign process.

    The foreign process receives only the reservation/stock references. Warehouse
    Management remains authoritative for on-hand and reserved quantities.
    """

    if quantity <= 0:
        raise ValueError("reservation quantity must be positive")
    normalized_quantity = round(float(quantity), 6)
    if normalized_quantity <= 0:
        raise ValueError("reservation quantity is below supported precision")
    if not sku:
        raise ValueError("reservation sku cannot be empty")
    if not reservation_reference:
        raise ValueError("reservation_reference cannot be empty")

    stock = _entity(persistence, "warehouse_management_stock", stock_id)
    stock_sku = str(stock.attributes.get("sku", ""))
    if stock_sku != sku:
        raise ValueError(
            f"reservation SKU mismatch: stock={stock_sku!r}, request={sku!r}"
        )

    reservations = dict(stock.attributes.get("external_reservations", {}))
    existing = reservations.get(reservation_reference)
    expected = {
        "sku": sku,
        "quantity": normalized_quantity,
        "consumed": False,
        "consumption_reference": None,
    }
    if existing is not None:
        existing_record = dict(existing)
        if (
            str(existing_record.get("sku")) != sku
            or float(existing_record.get("quantity", 0.0)) != normalized_quantity
        ):
            raise ValueError(
                f"reservation replay conflict: {reservation_reference}"
            )
        return False

    on_hand = float(stock.attributes.get("on_hand", 0.0))
    reserved = float(stock.attributes.get("reserved", 0.0))
    if on_hand - reserved + 1e-9 < normalized_quantity:
        return False

    stock.attributes["reserved"] = round(reserved + normalized_quantity, 6)
    reservations[reservation_reference] = expected
    stock.attributes["external_reservations"] = reservations

    event = engine.context.events.create(
        "warehouse_management.inventory_reserved",
        entity=stock,
        caused_by=caused_by,
        correlation_id=correlation_id,
        key=("warehouse-management", stock.id, "external-reservation", reservation_reference),
        reservation_reference=reservation_reference,
        sku=sku,
        quantity=normalized_quantity,
    )
    with persistence.transaction() as uow:
        uow.save_entity(stock)
        uow.append_event(event)
    return True


def consume_external_reservation(
    persistence: Persistence,
    engine: Engine,
    *,
    stock_id: str,
    quantity: float,
    sku: str,
    reservation_reference: str,
    consumption_reference: str,
    caused_by=None,
    correlation_id: str | None = None,
) -> bool:
    """Idempotently consume a previously reserved foreign-process quantity."""

    normalized_quantity = validated_consumption_request(
        quantity,
        sku=sku,
        reservation_reference=reservation_reference,
        consumption_reference=consumption_reference,
    )
    stock = _entity(persistence, "warehouse_management_stock", stock_id)
    stock_sku = str(stock.attributes.get("sku", ""))
    if stock_sku != sku:
        raise ValueError(
            f"consumption SKU mismatch: stock={stock_sku!r}, request={sku!r}"
        )

    reservations = dict(stock.attributes.get("external_reservations", {}))
    record = matching_reservation(
        reservations, reservation_reference, sku=sku, quantity=normalized_quantity,
    )
    if bool(record.get("consumed", False)):
        if record.get("consumption_reference") != consumption_reference:
            raise ValueError(
                f"consumption replay conflict: {reservation_reference}"
            )
        return False

    on_hand = float(stock.attributes.get("on_hand", 0.0))
    reserved = float(stock.attributes.get("reserved", 0.0))
    if reserved + 1e-9 < normalized_quantity or on_hand + 1e-9 < normalized_quantity:
        raise RuntimeError("reserved stock projection cannot satisfy consumption")

    stock.attributes["on_hand"] = round(on_hand - normalized_quantity, 6)
    stock.attributes["reserved"] = round(reserved - normalized_quantity, 6)
    record["consumed"] = True
    record["consumption_reference"] = consumption_reference
    reservations[reservation_reference] = record
    stock.attributes["external_reservations"] = reservations

    event = engine.context.events.create(
        "warehouse_management.inventory_consumed",
        entity=stock,
        caused_by=caused_by,
        correlation_id=correlation_id,
        key=(
            "warehouse-management",
            stock.id,
            "external-consumption",
            consumption_reference,
        ),
        reservation_reference=reservation_reference,
        consumption_reference=consumption_reference,
        sku=sku,
        quantity=normalized_quantity,
    )
    with persistence.transaction() as uow:
        uow.save_entity(stock)
        uow.append_event(event)
    return True
