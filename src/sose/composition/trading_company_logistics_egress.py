"""Logistics delivery-completion egress reconciler, split from trading_company_customer."""

from __future__ import annotations

from collections.abc import Mapping

from sose.core.identity import deterministic_id
from sose.persistence.memory import MemoryPersistence

from .boundary import BoundaryService
from .model import BoundaryMessage
from .trading_company_customer_support import (
    _consumer_effect_is_pending,
    _is_foreign_or_unconsumed,
    _next_logical_time,
    _operational_owner,
)


def _unique_sales_order_source(
    requests: list[BoundaryMessage], dispatch: BoundaryMessage, payload,
) -> BoundaryMessage:
    order_matches = [
        msg for msg in requests
        if msg.correlation_id == dispatch.correlation_id
        and msg.payload()["fulfillment_order_id"] == payload["fulfillment_order_id"]
    ]
    if len(order_matches) != 1:
        raise RuntimeError("delivered shipment lacks a unique durable sales order cause")
    return order_matches[0]


def _delivered_dispatch(
    uow,
    delivery,
    dispatch: BoundaryMessage | None,
    requests: list[BoundaryMessage],
    correlation_id: str | None,
) -> tuple[BoundaryMessage, str, str] | None:
    """The (dispatch, shipment, sales order) proven delivered, or None when not ready."""
    if _is_foreign_or_unconsumed(
        delivery, dispatch,
        contract_key="warehouse.dispatch_ready.v1", correlation_id=correlation_id,
    ):
        return None
    if _consumer_effect_is_pending(
        uow, delivery,
        missing_receipt="ACKed logistics dispatch is missing consumption evidence",
    ):
        return None
    payload = dispatch.payload()
    shipment_id = str(payload["shipment_id"])
    shipment = uow.get_entity("shipment", shipment_id)
    if shipment is None or shipment.state != "delivered":
        return None
    source = _unique_sales_order_source(requests, dispatch, payload)
    if (
        source.contract_key == "o2c.fulfillment_requested.v2"
        and source.payload().get("shipment_id") != shipment_id
    ):
        raise ValueError("Logistics egress contradicts v2 immutable shipment owner")
    return dispatch, shipment_id, str(source.payload()["order_id"])


def _delivered_dispatches(
    persistence: MemoryPersistence, correlation_id: str | None,
) -> list[tuple[BoundaryMessage, str, str]]:
    with persistence.transaction() as uow:
        inbound = tuple(
            (delivery, uow.get_boundary_message(delivery.message_id))
            for delivery in uow.boundary_deliveries()
        )
        requests = [
            msg for _, msg in inbound
            if msg is not None and msg.contract_key in {
                "o2c.fulfillment_requested.v1", "o2c.fulfillment_requested.v2",
            }
        ]
        ready = []
        for delivery, dispatch in inbound:
            item = _delivered_dispatch(uow, delivery, dispatch, requests, correlation_id)
            if item is not None:
                ready.append(item)
    return ready


def reconcile_completed_logistics_egress(
    persistence: MemoryPersistence, *, organization_owners: Mapping[str, str],
    correlation_id: str | None = None,
    max_new_messages: int | None = None,
    recovery_job_id: str | None = None,
) -> tuple[BoundaryMessage, ...]:
    """Recover Logistics delivery completion solely from committed domain truth.

    The dispatch ACK alone is not a delivered shipment. Publish downstream
    only after the accepted Logistics Command has been executed/removed and
    the persisted shipment is actually in the delivered state.
    """
    if max_new_messages is not None and max_new_messages < 0:
        raise ValueError("max_new_messages must be >= 0")

    ready = _delivered_dispatches(persistence, correlation_id)

    service = BoundaryService(persistence)
    outgoing: list[BoundaryMessage] = []
    for dispatch, shipment_id, sales_order_id in sorted(ready, key=lambda item: item[0].message_id):
        identity = deterministic_id(
            "boundary-message", "logistics", shipment_id,
            "logistics.delivery_completed", 1, "order_to_cash", "delivery-completed",
        )
        with persistence.transaction() as uow:
            prior = uow.get_boundary_message(identity)
        produced_at = (
            prior.produced_at if prior is not None
            else _next_logical_time(
                persistence, dispatch.produced_at,
                organization_id=_operational_owner(organization_owners, dispatch.correlation_id),
            )
        )
        message = BoundaryMessage.create(
            contract_name="logistics.delivery_completed", contract_version=1,
            source_domain="logistics", source_identity=shipment_id,
            destination_domain="order_to_cash", occurrence_key="delivery-completed",
            correlation_id=dispatch.correlation_id, causation_id=dispatch.message_id,
            produced_at=produced_at,
            payload={"shipment_id": shipment_id, "order_id": sales_order_id},
        )
        if prior is not None:
            if prior != message:
                raise ValueError("durable Logistics egress conflicts with source facts")
            outgoing.append(prior)
            continue
        if max_new_messages is not None and max_new_messages == 0:
            break
        if max_new_messages is not None:
            max_new_messages -= 1
        service.publish(message, recovery_job_id=recovery_job_id)
        outgoing.append(message)
    return tuple(outgoing)
