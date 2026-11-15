"""Warehouse Fulfillment egress reconciler, split from trading_company_customer."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from sose.core.identity import deterministic_id
from sose.persistence.memory import MemoryPersistence

from sose.examples.warehouse_fulfillment import simulation as fulfillment

from .boundary import BoundaryService
from .model import BoundaryMessage, DeliveryStatus
from .trading_company_customer_support import _next_logical_time, _operational_owner


class _FulfillmentEgressPublisher:
    """Publishes or verifies outbound WF contracts under one shared message budget."""

    def __init__(
        self,
        persistence: MemoryPersistence,
        *,
        remaining: int | None,
        recovery_job_id: str | None,
        publish: Callable[..., BoundaryMessage],
    ) -> None:
        self._persistence = persistence
        self._remaining = remaining
        self._recovery_job_id = recovery_job_id
        self._publish = publish
        self._service = BoundaryService(persistence)

    def publish_or_verify(
        self,
        *, contract_name: str, destination_domain: str, occurrence_key: str,
        source_identity: str, correlation: str, causation: str, produced_at: datetime,
        payload: dict[str, object],
    ) -> BoundaryMessage | None:
        message_id = deterministic_id(
            "boundary-message", "warehouse_fulfillment", source_identity,
            contract_name, 1, destination_domain, occurrence_key,
        )
        with self._persistence.transaction() as uow:
            prior = uow.get_boundary_message(message_id)
        if prior is not None:
            expected = BoundaryMessage.create(
                contract_name=contract_name, contract_version=1,
                source_domain="warehouse_fulfillment", source_identity=source_identity,
                destination_domain=destination_domain, occurrence_key=occurrence_key,
                correlation_id=correlation, causation_id=causation,
                produced_at=prior.produced_at, payload=payload,
            )
            if prior != expected:
                raise ValueError("durable outbound contract conflicts with source facts")
            return prior
        if self._remaining is not None:
            if self._remaining == 0:
                return None
            self._remaining -= 1
        return self._publish(
            self._service, contract_name=contract_name,
            source_domain="warehouse_fulfillment", source_identity=source_identity,
            destination_domain=destination_domain, occurrence_key=occurrence_key,
            correlation_id=correlation, causation_id=causation,
            produced_at=produced_at, payload=payload,
            recovery_job_id=self._recovery_job_id,
        )


def _accepted_inbound(
    persistence: MemoryPersistence,
) -> tuple[tuple[BoundaryMessage, ...], set[str]]:
    with persistence.transaction() as uow:
        inbound = tuple(
            (delivery, message)
            for delivery in uow.boundary_deliveries()
            if (message := uow.get_boundary_message(delivery.message_id)) is not None
        )
        accepted = {
            delivery.message_id
            for delivery, message in inbound
            if delivery.status is DeliveryStatus.CONSUMED
            and uow.get_boundary_consumption(delivery.delivery_id) is not None
        }
    messages = tuple(message for _, message in inbound)
    return messages, accepted


def _requests_and_reservations(
    messages: tuple[BoundaryMessage, ...], accepted: set[str], correlation_id: str | None,
) -> tuple[list[BoundaryMessage], list[BoundaryMessage]]:
    requests = [
        message for message in messages
        if message.contract_key in {
            "o2c.fulfillment_requested.v1", "o2c.fulfillment_requested.v2",
        }
    ]
    reservations = [
        message for message in messages
        if message.contract_key == "warehouse.inventory_reserved.v1"
        and message.message_id in accepted
        and (correlation_id is None or message.correlation_id == correlation_id)
    ]
    return requests, reservations


def _v2_shipment_id(
    persistence: MemoryPersistence, request: BoundaryMessage, requests: list[BoundaryMessage],
) -> str:
    shipment_id = request.payload().get("shipment_id")
    if not isinstance(shipment_id, str) or not shipment_id:
        raise ValueError("v2 fulfillment request lacks shipment identity")
    if persistence.entity("shipment", shipment_id) is None:
        raise ValueError("v2 fulfillment request references missing shipment")
    if any(
        other.message_id != request.message_id
        and other.contract_key == "o2c.fulfillment_requested.v2"
        and other.payload().get("shipment_id") == shipment_id
        for other in requests
    ):
        raise ValueError("v2 shipment has multiple fulfillment owners")
    return shipment_id


def _legacy_shipment_id(persistence: MemoryPersistence) -> str:
    shipment_ids = [
        entity.id for entity in persistence.entities()
        if entity.entity_type == "shipment"
    ]
    if len(shipment_ids) != 1:
        raise ValueError("legacy v1 shipment linkage is ambiguous")
    return shipment_ids[0]


def _request_shipment_id(
    persistence: MemoryPersistence,
    requests: list[BoundaryMessage],
    reservation: BoundaryMessage,
    order_id: str,
) -> str:
    matching = [
        request for request in requests
        if request.correlation_id == reservation.correlation_id
        and request.payload().get("fulfillment_order_id") == order_id
    ]
    if len(matching) != 1:
        raise ValueError("shipped fulfillment lacks unique durable O2C request")
    # Contract v2 binds one shipment directly to immutable ingress.
    # Frozen v1 stays opaque and requires an unambiguous single shipment.
    request = matching[0]
    if request.contract_key == "o2c.fulfillment_requested.v2":
        return _v2_shipment_id(persistence, request, requests)
    return _legacy_shipment_id(persistence)


def _order_owned_allocation(
    persistence: MemoryPersistence,
    allocation_ids: tuple,
    *,
    stock_id: str,
    reservation_reference: str,
):
    """The single order-owned WM allocation, or None when it does not match the reservation."""
    if len(allocation_ids) != 1:
        raise ValueError("shipped composed order needs one durable WM allocation")
    allocation = persistence.entity("warehouse_allocation", str(allocation_ids[0]))
    if allocation is None or any(
        allocation.attributes.get(name) != value
        for name, value in {
            "inventory_owner": "warehouse_management",
            "reservation_reference": reservation_reference,
            "stock_reference": stock_id,
        }.items()
    ):
        # Only the order-owned allocation can cause stock consumption.
        return None
    return allocation


def _pick_is_committed(
    persistence: MemoryPersistence,
    allocation,
    allocation_ids: tuple,
    reservation_reference: str,
) -> bool:
    all_allocations = [
        persistence.entity("warehouse_allocation", str(aid))
        for aid in allocation_ids
    ]
    if not all_allocations or any(
        a is None or a.state not in {"picked", "shipped"}
        for a in all_allocations
    ):
        return False
    pick_occurrence = persistence.entity(
        "warehouse_inventory_occurrence",
        fulfillment.occurrence_id("pick", allocation.id, 1),
    )
    if (
        pick_occurrence is None
        or pick_occurrence.state != "committed"
        or pick_occurrence.attributes.get("reservation_reference") != reservation_reference
    ):
        return False
    return True


def _publish_dispatch_if_consumed(
    persistence: MemoryPersistence,
    publisher: _FulfillmentEgressPublisher,
    outgoing: list[BoundaryMessage],
    *,
    organization_owners: Mapping[str, str],
    reservation: BoundaryMessage,
    consumption: BoundaryMessage,
    order,
    order_id: str,
    stock_id: str,
    reservation_reference: str,
    shipment_id: str,
) -> bool:
    """Publish dispatch after WM consumption; False stops the whole reconciliation."""
    # Durable pick completion authorizes consumption, but Logistics
    # dispatch is *separately* gated on terminal shipment and WM-owned
    # consumed reservation truth.
    if order.state != "shipped":
        return True
    stock = persistence.entity("warehouse_management_stock", stock_id)
    stock_reservations = (
        {} if stock is None else dict(stock.attributes.get("external_reservations", {}))
    )
    consumed = stock_reservations.get(reservation_reference, {}).get("consumed")
    if consumed is not True:
        return True

    dispatch = publisher.publish_or_verify(
        contract_name="warehouse.dispatch_ready",
        destination_domain="logistics",
        occurrence_key="dispatch-ready",
        source_identity=order_id,
        correlation=reservation.correlation_id,
        causation=consumption.message_id,
        produced_at=_next_logical_time(
            persistence, consumption.produced_at,
            organization_id=_operational_owner(organization_owners, consumption.correlation_id),
        ),
        payload={"fulfillment_order_id": order_id, "shipment_id": shipment_id},
    )
    if dispatch is None:
        return False
    outgoing.append(dispatch)
    return True


def _reconcile_reservation(
    persistence: MemoryPersistence,
    publisher: _FulfillmentEgressPublisher,
    outgoing: list[BoundaryMessage],
    *,
    organization_owners: Mapping[str, str],
    requests: list[BoundaryMessage],
    reservation: BoundaryMessage,
) -> bool:
    """Emit the egress of one accepted reservation; False stops the whole reconciliation."""
    details = reservation.payload()
    order_id = str(details["fulfillment_order_id"])
    order = persistence.entity("warehouse_fulfillment_order", order_id)
    # Consumption is a durable *pick-completion* effect, not a shipment
    # effect. WF remains in "picking" until packed but its allocation and
    # committed pick occurrence prove actual completion.
    if order is None or order.state not in {"picking", "packed", "shipped"}:
        return True
    shipment_id = _request_shipment_id(persistence, requests, reservation, order_id)
    stock_id = str(details["stock_id"])
    reservation_reference = str(details["reservation_reference"])
    allocation_ids = tuple(order.attributes.get("allocation_ids", ()))
    allocation = _order_owned_allocation(
        persistence, allocation_ids,
        stock_id=stock_id, reservation_reference=reservation_reference,
    )
    if allocation is None:
        return True
    if not _pick_is_committed(persistence, allocation, allocation_ids, reservation_reference):
        return True
    sku = str(details["sku"])
    quantity = float(details["quantity"])
    if (
        sku != order.attributes.get("requested_sku")
        or quantity != float(order.attributes.get("requested_quantity", -1))
    ):
        raise ValueError("reservation does not reconcile to shipped fulfillment")
    consumption_reference = deterministic_id(
        "trading-company-inventory-consumption", reservation_reference,
    )
    consumption = publisher.publish_or_verify(
        contract_name="warehouse.inventory_consumption_requested",
        destination_domain="warehouse_management",
        occurrence_key="inventory-consumption-requested",
        source_identity=order_id,
        correlation=reservation.correlation_id,
        causation=reservation.message_id,
        produced_at=_next_logical_time(
            persistence, reservation.produced_at,
            organization_id=_operational_owner(organization_owners, reservation.correlation_id),
        ),
        payload={
            "fulfillment_order_id": order_id,
            "stock_id": stock_id,
            "reservation_reference": reservation_reference,
            "consumption_reference": consumption_reference,
            "sku": sku,
            "quantity": quantity,
        },
    )
    if consumption is None:
        return False
    outgoing.append(consumption)
    return _publish_dispatch_if_consumed(
        persistence, publisher, outgoing,
        organization_owners=organization_owners, reservation=reservation,
        consumption=consumption, order=order, order_id=order_id, stock_id=stock_id,
        reservation_reference=reservation_reference, shipment_id=shipment_id,
    )


def _reconcile_shipped_fulfillment_egress(
    persistence: MemoryPersistence, *, organization_owners: Mapping[str, str],
    correlation_id: str | None,
    max_new_messages: int | None,
    recovery_job_id: str | None,
    publish: Callable[..., BoundaryMessage],
) -> tuple[BoundaryMessage, ...]:
    """Body of `reconcile_shipped_fulfillment_egress`; `publish` is the caller's `_publish`."""
    if max_new_messages is not None and max_new_messages < 0:
        raise ValueError("max_new_messages must be >= 0")
    messages, accepted = _accepted_inbound(persistence)
    requests, reservations = _requests_and_reservations(messages, accepted, correlation_id)
    publisher = _FulfillmentEgressPublisher(
        persistence, remaining=max_new_messages,
        recovery_job_id=recovery_job_id, publish=publish,
    )
    outgoing: list[BoundaryMessage] = []
    for reservation in sorted(reservations, key=lambda m: m.message_id):
        if not _reconcile_reservation(
            persistence, publisher, outgoing,
            organization_owners=organization_owners,
            requests=requests, reservation=reservation,
        ):
            break
    return tuple(outgoing)
