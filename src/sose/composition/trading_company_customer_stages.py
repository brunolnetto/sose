"""Seeding and stage payloads of the customer-demand path, split from trading_company_customer."""

from __future__ import annotations

from datetime import datetime

from sose.core.identity import deterministic_id
from sose.persistence.memory import MemoryPersistence

from sose.examples.order_to_cash import simulation as o2c
from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.examples.logistics import simulation as logistics
from sose.examples.cards_payments import simulation as payments
from sose.examples.record_to_report import simulation as r2r
from sose.examples.warehouse_management import simulation as wm

from .trading_company_customer_support import _CustomerFixtures


def _require_demand_path_arguments(
    organization_id: str, persistence: MemoryPersistence | None, instance_key: str | None,
) -> None:
    if not isinstance(organization_id, str) or not organization_id.strip():
        raise ValueError("organization_id must not be empty")
    if persistence is not None and instance_key is None:
        raise ValueError("instance_key is required for injected persistence")
    if instance_key is not None and (not isinstance(instance_key, str) or not instance_key.strip()):
        raise ValueError("instance_key must be a nonempty string")


def _demand_origin(persistence: MemoryPersistence, organization_id: str) -> datetime:
    position = persistence.simulation_position(owner_id=organization_id)
    return max(
        o2c.ORIGIN, position.logical_time if position is not None else o2c.ORIGIN,
    )


def _require_uninitialized_instance(
    persistence: MemoryPersistence, instance_key: str | None,
) -> None:
    if instance_key is not None:
        order_id = deterministic_id(
            "entity", "sales_order", "o2c-reference", "order-1", instance_key,
        )
        if persistence.entity("sales_order", order_id) is not None:
            raise ValueError(
                "customer instance already initialized: use durable recovery instead of reseeding"
            )


def _seed_customer_fixtures(
    persistence: MemoryPersistence,
    *,
    organization_id: str,
    origin: datetime,
    amount: float,
    currency: str,
    instance_key: str | None,
) -> _CustomerFixtures:
    sales_order = o2c.seed_reference(
        persistence, owner_id=organization_id,
        now=origin, amount=amount, currency=currency,
        instance_key=instance_key,
    )
    fulfillment_order_id = deterministic_id(
        "entity", "warehouse_fulfillment_order",
        "warehouse-reference", f"order:{sales_order.order_id}",
    )
    return _CustomerFixtures(
        o2c=sales_order,
        # A reference only: the actual order is created by the durable ingress
        # intent from the O2C boundary payload.
        fulfillment=fulfillment.WarehouseEntities(
            order_id=fulfillment_order_id, lot_ids=(),
        ),
        warehouse=wm.seed_reference(
            persistence,
            owner_id=organization_id,
            now=origin,
            origin_on_hand=20.0,
            transfer_quantity=1.0,
            sku=fulfillment.PRIMARY_SKU,
            instance_key=instance_key,
        ),
        logistics=logistics.seed_reference(
            persistence,
            owner_id=organization_id,
            now=origin,
            instance_key=instance_key,
        ),
        payments=payments.seed_reference(
            persistence,
            owner_id=organization_id,
            now=origin,
            amount=amount,
            currency=currency,
            instance_key=instance_key,
        ),
        r2r=r2r.seed_reference(
            persistence,
            owner_id=organization_id,
            now=origin,
            amount=amount,
            currency=currency,
            instance_key=instance_key,
        ),
    )


def _fulfillment_requested_stage(
    fixtures: _CustomerFixtures, *, requested_quantity: float, instance_key: str | None,
) -> dict[str, object]:
    return dict(
        contract_name="o2c.fulfillment_requested",
        contract_version=2 if instance_key is not None else 1,
        source_domain="order_to_cash",
        source_identity=fixtures.o2c.order_id,
        destination_domain="warehouse_fulfillment",
        occurrence_key="fulfillment-requested",
        owner_id="warehouse-fulfillment-worker",
        payload={
            "order_id": fixtures.o2c.order_id,
            "fulfillment_order_id": fixtures.fulfillment.order_id,
            "requested_quantity": requested_quantity,
            "sku": fulfillment.PRIMARY_SKU,
            **({"shipment_id": fixtures.logistics.shipment_id}
               if instance_key is not None else {}),
        },
    )


def _reservation_requested_stage(
    fixtures: _CustomerFixtures, *, reservation_reference: str, requested_quantity: float,
) -> dict[str, object]:
    return dict(
        contract_name="warehouse.inventory_reservation_requested",
        source_domain="warehouse_fulfillment",
        source_identity=fixtures.fulfillment.order_id,
        destination_domain="warehouse_management",
        occurrence_key="inventory-reservation-requested",
        owner_id="warehouse-management-worker",
        payload={
            "fulfillment_order_id": fixtures.fulfillment.order_id,
            "stock_id": fixtures.warehouse.origin_stock_id,
            "reservation_reference": reservation_reference,
            "sku": fulfillment.PRIMARY_SKU,
            "quantity": requested_quantity,
        },
    )


def _inventory_reserved_stage(
    fixtures: _CustomerFixtures, *, reservation_reference: str, requested_quantity: float,
) -> dict[str, object]:
    return dict(
        contract_name="warehouse.inventory_reserved",
        source_domain="warehouse_management",
        source_identity=fixtures.warehouse.origin_stock_id,
        destination_domain="warehouse_fulfillment",
        occurrence_key="inventory-reserved",
        owner_id="warehouse-fulfillment-worker",
        payload={
            "fulfillment_order_id": fixtures.fulfillment.order_id,
            "stock_id": fixtures.warehouse.origin_stock_id,
            "reservation_reference": reservation_reference,
            "sku": fulfillment.PRIMARY_SKU,
            "quantity": requested_quantity,
        },
    )


def _delivery_completed_stage(fixtures: _CustomerFixtures) -> dict[str, object]:
    return dict(
        contract_name="logistics.delivery_completed",
        source_domain="logistics",
        source_identity=fixtures.logistics.shipment_id,
        destination_domain="order_to_cash",
        occurrence_key="delivery-completed",
        owner_id="o2c-worker",
        payload={
            "shipment_id": fixtures.logistics.shipment_id,
            "order_id": fixtures.o2c.order_id,
        },
    )


def _payment_requested_stage(
    fixtures: _CustomerFixtures, *, amount: float, currency: str,
) -> dict[str, object]:
    return dict(
        contract_name="o2c.payment_requested",
        source_domain="order_to_cash",
        source_identity=fixtures.o2c.order_id,
        destination_domain="cards_payments",
        occurrence_key="payment-requested",
        owner_id="payments-worker",
        payload={
            "order_id": fixtures.o2c.order_id,
            "payment_id": fixtures.payments.payment_id,
            "amount": amount,
            "currency": currency,
        },
    )


def _journal_requested_stage(
    fixtures: _CustomerFixtures, *, amount: float, currency: str,
) -> dict[str, object]:
    return dict(
        contract_name="accounting.entry_requested",
        source_domain="cards_payments",
        source_identity=fixtures.payments.payment_id,
        destination_domain="record_to_report",
        occurrence_key="customer-settlement-entry",
        owner_id="r2r-worker",
        payload={
            "payment_id": fixtures.payments.payment_id,
            "journal_id": fixtures.r2r.journal_id,
            "amount": amount,
            "currency": currency,
        },
    )
