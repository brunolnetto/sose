"""Boundary contracts, pending-effect scan and replay of the PC6 recovery runner.

Split from recovery.py.
"""
from __future__ import annotations

from sose.core.resource_reservations import TemporalReservation

from sose.composition import trading_company_customer as customer
from sose.composition.boundary import BoundaryConsumerRegistry
from sose.composition.model import BoundaryMessage, DeliveryStatus
from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.examples.logistics import simulation as logistics
from sose.examples.order_to_cash import simulation as o2c
from sose.examples.cards_payments import simulation as payments
from sose.examples.record_to_report import simulation as r2r
from sose.persistence.base import Persistence


_WM_CONTRACT = ("warehouse.inventory_consumption_requested", 1)
_LOGISTICS_CONTRACT = ("warehouse.dispatch_ready", 1)
_O2C_CONTRACT = ("logistics.delivery_completed", 1)
_PAYMENTS_CONTRACT = ("o2c.payment_requested", 1)
_R2R_CONTRACT = ("accounting.entry_requested", 1)
_RECOVERABLE = frozenset({
    "composition.accept_inventory_reservation",
    "composition.consume_fulfillment_inventory",
    "composition.deliver_shipment",
    "composition.complete_external_fulfillment",
    "composition.settle_customer_payment",
    "composition.post_customer_journal",
    "composition.post_replenishment_journal",
})


def pending_effects(store: Persistence) -> tuple[tuple[BoundaryMessage, str], ...]:
    with store.transaction() as uow:
        pending = []
        for delivery in uow.boundary_deliveries():
            if delivery.status is not DeliveryStatus.CONSUMED:
                continue
            consumption = uow.get_boundary_consumption(delivery.delivery_id)
            if consumption is None:
                raise RuntimeError("consumed boundary delivery lacks durable consumption")
            intent = uow.get_command(consumption.consumer_effect_id)
            if intent is None or intent.name not in _RECOVERABLE:
                continue
            message = uow.get_boundary_message(delivery.message_id)
            if message is None:
                raise RuntimeError("accepted boundary effect lacks source message")
            pending.append((message, intent.command_id))
    return tuple(sorted(pending, key=lambda pair: (pair[0].produced_at, pair[0].message_id)))


def execute_pending(
    store: Persistence, source: BoundaryMessage, effect_id: str,
    *, organization_id: str,
    authoritative_pickup: TemporalReservation | None = None,
    resource_persistence: Persistence | None = None,
) -> None:
    details = source.payload()
    order_id = str(details.get("fulfillment_order_id", ""))
    sales_order_id = str(details.get("order_id", ""))
    payment_id = str(details.get("payment_id", ""))
    journal_id = str(details.get("journal_id", ""))
    with store.transaction() as uow:
        journal = uow.get_entity("journal_entry", journal_id) if journal_id else None
    period_id = str(journal.attributes["period_id"]) if journal is not None else ""
    shipment_id = str(details.get("shipment_id", ""))
    fixture = customer._CustomerFixtures(
        o2c=o2c.O2CEntities(order_id=sales_order_id),
        fulfillment=fulfillment.WarehouseEntities(order_id=order_id, lot_ids=()),
        warehouse=None,
        logistics=logistics.LogisticsEntities(shipment_id=shipment_id),
        payments=payments.PaymentEntities(payment_id=payment_id),
        r2r=r2r.R2REntities(
            period_id=period_id, journal_id=journal_id,
            reconciliation_id="", close_task_id="",
        ),
    )
    kwargs = (
        {"authoritative_pickup": authoritative_pickup}
        if authoritative_pickup is not None else {}
    )
    if resource_persistence is not None and authoritative_pickup is not None:
        kwargs["resource_persistence"] = resource_persistence
    customer._execute_intent(
        store,
        effect_id=effect_id,
        fixtures=fixture,
        correlation_id=source.correlation_id,
        organization_id=organization_id,
        **kwargs,
    )


def registry_for(message: BoundaryMessage, *, organization_id: str) -> BoundaryConsumerRegistry:
    details = message.payload()
    registry = BoundaryConsumerRegistry()
    if message.contract_key == "warehouse.inventory_consumption_requested.v1":
        registry.register(
            destination_domain="warehouse_management",
            contract_name=_WM_CONTRACT[0],
            contract_version=_WM_CONTRACT[1],
            handler=customer._intent_handler(
                intent_name="composition.consume_fulfillment_inventory",
                entity_type="warehouse_management_stock",
                entity_id=str(details["stock_id"]),
                owner_id=organization_id,
            ),
        )
    elif message.contract_key == "warehouse.dispatch_ready.v1":
        registry.register(
            destination_domain="logistics",
            contract_name=_LOGISTICS_CONTRACT[0],
            contract_version=_LOGISTICS_CONTRACT[1],
            handler=customer._intent_handler(
                intent_name="composition.deliver_shipment",
                entity_type="shipment",
                entity_id=str(details["shipment_id"]),
                owner_id=organization_id,
            ),
        )
    elif message.contract_key == "logistics.delivery_completed.v1":
        registry.register(
            destination_domain="order_to_cash",
            contract_name=_O2C_CONTRACT[0],
            contract_version=_O2C_CONTRACT[1],
            handler=customer._intent_handler(
                intent_name="composition.complete_external_fulfillment",
                entity_type="sales_order",
                entity_id=str(details["order_id"]),
                owner_id=organization_id,
            ),
        )
    elif message.contract_key == "o2c.payment_requested.v1":
        registry.register(
            destination_domain="cards_payments",
            contract_name=_PAYMENTS_CONTRACT[0],
            contract_version=_PAYMENTS_CONTRACT[1],
            handler=customer._intent_handler(
                intent_name="composition.settle_customer_payment",
                entity_type="card_payment",
                entity_id=str(details["payment_id"]),
                owner_id=organization_id,
            ),
        )
    elif message.contract_key == "accounting.entry_requested.v1":
        if message.source_domain == "cards_payments":
            intent_name = "composition.post_customer_journal"
        elif message.source_domain == "procure_to_pay":
            intent_name = "composition.post_replenishment_journal"
        else:
            raise ValueError("unsupported accounting source domain for PC6 recovery")
        registry.register(
            destination_domain="record_to_report",
            contract_name=_R2R_CONTRACT[0],
            contract_version=_R2R_CONTRACT[1],
            handler=customer._intent_handler(
                intent_name=intent_name,
                entity_type="journal_entry",
                entity_id=str(details["journal_id"]),
                owner_id=organization_id,
            ),
        )
    else:
        raise ValueError("recovery runner received unsupported boundary contract")
    return registry
