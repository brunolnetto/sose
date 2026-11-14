"""Validation of external stock reservation consumption (warehouse management)."""
from __future__ import annotations


def validated_consumption_request(
    quantity: float,
    *,
    sku: str,
    reservation_reference: str,
    consumption_reference: str,
) -> float:
    """Validate a consumption request; returns the 6-decimal normalized quantity."""
    if quantity <= 0:
        raise ValueError("consumption quantity must be positive")
    normalized_quantity = round(float(quantity), 6)
    if normalized_quantity <= 0:
        raise ValueError("consumption quantity is below supported precision")
    if not sku:
        raise ValueError("consumption sku cannot be empty")
    if not reservation_reference:
        raise ValueError("reservation_reference cannot be empty")
    if not consumption_reference:
        raise ValueError("consumption_reference cannot be empty")
    return normalized_quantity


def matching_reservation(
    reservations: dict,
    reservation_reference: str,
    *,
    sku: str,
    quantity: float,
) -> dict:
    """A copy of the named reservation, which must match the requested SKU and quantity."""
    existing = reservations.get(reservation_reference)
    if existing is None:
        raise ValueError(f"unknown external reservation: {reservation_reference}")
    record = dict(existing)
    if (
        str(record.get("sku")) != sku
        or float(record.get("quantity", 0.0)) != quantity
    ):
        raise ValueError(
            f"reservation consumption conflict: {reservation_reference}"
        )
    return record
