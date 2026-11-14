"""O2C and settled-payment egress reconcilers, split from trading_company_customer."""

from __future__ import annotations

from collections.abc import Mapping

from sose.core.identity import deterministic_id
from sose.persistence.memory import MemoryPersistence

from sose.examples.order_to_cash import simulation as o2c

from .boundary import BoundaryService
from .model import BoundaryMessage
from .trading_company_customer_support import (
    _consumer_effect_is_pending,
    _is_foreign_or_unconsumed,
    _next_logical_time,
    _operational_owner,
)


def _invoiced_order_terms(uow, order_id: str) -> tuple[object, object] | None:
    """Amount and currency of an invoiced order with an open receivable, else None."""
    order = uow.get_entity("sales_order", order_id)
    if order is None or order.state != "invoiced":
        return None
    receivable = uow.get_entity("receivable", o2c.receivable_id(order_id))
    if receivable is None or receivable.state != "open":
        return None
    amount = order.attributes["amount"]
    currency = order.attributes["currency"]
    if (
        receivable.attributes.get("order_id") != order_id
        or receivable.attributes.get("amount") != amount
        or receivable.attributes.get("currency") != currency
    ):
        raise ValueError("receivable does not certify the invoiced sales order")
    return amount, currency


def _bound_payment_id(uow, message: BoundaryMessage, order_id: str, amount, currency) -> str:
    binding = uow.get_customer_settlement_binding("order", order_id)
    if binding is None:
        raise RuntimeError("O2C payment destination lacks explicit durable settlement binding")
    if (
        binding.correlation_id != message.correlation_id
        or binding.amount != amount
        or binding.currency != currency
    ):
        raise ValueError("O2C settlement binding contradicts the causally invoiced order")
    bound_payment = uow.get_entity("card_payment", binding.payment_id)
    if bound_payment is None:
        raise RuntimeError("O2C bound card payment is missing")
    if (
        bound_payment.attributes.get("amount") != amount
        or bound_payment.attributes.get("currency") != currency
    ):
        raise ValueError("O2C bound card payment conflicts with the invoiced order")
    return binding.payment_id


def _invoiced_delivery(
    uow, delivery, message: BoundaryMessage | None, correlation_id: str | None,
) -> tuple | None:
    """The payment request facts proven by one consumed delivery, or None when not ready."""
    if _is_foreign_or_unconsumed(
        delivery, message,
        contract_key="logistics.delivery_completed.v1", correlation_id=correlation_id,
    ):
        return None
    if _consumer_effect_is_pending(
        uow, delivery, missing_receipt="ACKed O2C delivery is missing consumption evidence",
    ):
        return None
    order_id = str(message.payload()["order_id"])
    terms = _invoiced_order_terms(uow, order_id)
    if terms is None:
        return None
    amount, currency = terms
    payment_id = _bound_payment_id(uow, message, order_id, amount, currency)
    return message, order_id, amount, currency, payment_id


def _invoiced_deliveries(persistence: MemoryPersistence, correlation_id: str | None) -> list:
    with persistence.transaction() as uow:
        inbound = tuple(
            (delivery, uow.get_boundary_message(delivery.message_id))
            for delivery in uow.boundary_deliveries()
        )
        ready = []
        for delivery, message in inbound:
            item = _invoiced_delivery(uow, delivery, message, correlation_id)
            if item is not None:
                ready.append(item)
    return ready


def reconcile_invoiced_o2c_egress(
    persistence: MemoryPersistence, *, organization_owners: Mapping[str, str],
    correlation_id: str | None = None,
    max_new_messages: int | None = None,
    recovery_job_id: str | None = None,
) -> tuple[BoundaryMessage, ...]:
    """Derive payment requests from committed O2C invoicing and causal ACKs.

    Do not mistake the Logistics receipt for applied O2C state, and do not
    infer a payment ID when the durable cardinality is ambiguous. This is the
    PC6 reference-domain contract, not a generic payment-routing heuristic.
    """
    if max_new_messages is not None and max_new_messages < 0:
        raise ValueError("max_new_messages must be >= 0")

    ready = _invoiced_deliveries(persistence, correlation_id)

    service = BoundaryService(persistence)
    emitted: list[BoundaryMessage] = []
    for upstream, order_id, amount, currency, payment_id in sorted(
        ready, key=lambda item: item[0].message_id
    ):
        identity = deterministic_id(
            "boundary-message", "order_to_cash", order_id,
            "o2c.payment_requested", 1, "cards_payments", "payment-requested",
        )
        with persistence.transaction() as uow:
            previous = uow.get_boundary_message(identity)
        produced_at = (
            previous.produced_at if previous is not None
            else _next_logical_time(
                persistence, upstream.produced_at,
                organization_id=_operational_owner(organization_owners, upstream.correlation_id),
            )
        )
        outgoing = BoundaryMessage.create(
            contract_name="o2c.payment_requested", contract_version=1,
            source_domain="order_to_cash", source_identity=order_id,
            destination_domain="cards_payments", occurrence_key="payment-requested",
            correlation_id=upstream.correlation_id,
            causation_id=upstream.message_id, produced_at=produced_at,
            payload={
                "order_id": order_id, "payment_id": payment_id,
                "amount": amount, "currency": currency,
            },
        )
        if previous is not None:
            if previous != outgoing:
                raise ValueError("durable O2C payment request conflicts with source facts")
            emitted.append(previous)
            continue
        if max_new_messages is not None and max_new_messages == 0:
            break
        service.publish(outgoing, recovery_job_id=recovery_job_id)
        if max_new_messages is not None:
            max_new_messages -= 1
        emitted.append(outgoing)
    return tuple(emitted)


def _bound_journal_id(uow, upstream: BoundaryMessage, payload, payment_id: str) -> str:
    binding = uow.get_customer_settlement_binding("payment", payment_id)
    if binding is None:
        raise RuntimeError("settlement journal lacks explicit durable accounting binding")
    if (
        binding.order_id != payload["order_id"]
        or binding.correlation_id != upstream.correlation_id
        or binding.amount != payload["amount"]
        or binding.currency != payload["currency"]
    ):
        raise ValueError("settled payment contradicts its immutable accounting binding")
    bound_journal = uow.get_entity("journal_entry", binding.journal_id)
    if bound_journal is None:
        raise RuntimeError("settlement bound journal is missing")
    if (
        bound_journal.attributes.get("amount") != binding.amount
        or bound_journal.attributes.get("currency") != binding.currency
    ):
        raise ValueError("settlement bound journal conflicts with original payment")
    return binding.journal_id


def _settled_payment_request(
    uow, delivery, upstream: BoundaryMessage | None, correlation_id: str | None,
) -> tuple | None:
    """The journal request facts proven by one consumed delivery, or None when not ready."""
    if _is_foreign_or_unconsumed(
        delivery, upstream,
        contract_key="o2c.payment_requested.v1", correlation_id=correlation_id,
    ):
        return None
    if _consumer_effect_is_pending(
        uow, delivery,
        missing_receipt="settlement egress requires durable payment ACK receipt",
    ):
        return None
    payload = upstream.payload()
    payment_id = str(payload["payment_id"])
    payment = uow.get_entity("card_payment", payment_id)
    if payment is None or payment.state != "settled":
        return None
    if (
        payment.attributes.get("amount") != payload["amount"]
        or payment.attributes.get("currency") != payload["currency"]
    ):
        raise ValueError("settled card payment conflicts with committed request")
    journal_id = _bound_journal_id(uow, upstream, payload, payment_id)
    return (
        upstream, payment_id, payload["amount"], payload["currency"],
        journal_id,
    )


def _settled_payment_requests(persistence: MemoryPersistence, correlation_id: str | None) -> list:
    with persistence.transaction() as uow:
        ready = []
        for delivery in uow.boundary_deliveries():
            upstream = uow.get_boundary_message(delivery.message_id)
            item = _settled_payment_request(uow, delivery, upstream, correlation_id)
            if item is not None:
                ready.append(item)
    return ready


def reconcile_settled_payment_egress(
    persistence: MemoryPersistence, *, organization_owners: Mapping[str, str],
    correlation_id: str | None = None,
    max_new_messages: int | None = None,
    recovery_job_id: str | None = None,
) -> tuple[BoundaryMessage, ...]:
    """Emit R2R work only after committed payment settlement business truth."""
    if max_new_messages is not None and max_new_messages < 0:
        raise ValueError("max_new_messages must be >= 0")
    ready = _settled_payment_requests(persistence, correlation_id)

    service = BoundaryService(persistence)
    results: list[BoundaryMessage] = []
    for upstream, payment_id, amount, currency, journal_id in sorted(
        ready, key=lambda item: item[0].message_id
    ):
        identity = deterministic_id(
            "boundary-message", "cards_payments", payment_id,
            "accounting.entry_requested", 1, "record_to_report",
            "customer-settlement-entry",
        )
        with persistence.transaction() as uow:
            existing = uow.get_boundary_message(identity)
        produced_at = (
            existing.produced_at if existing is not None
            else _next_logical_time(
                persistence, upstream.produced_at,
                organization_id=_operational_owner(organization_owners, upstream.correlation_id),
            )
        )
        message = BoundaryMessage.create(
            contract_name="accounting.entry_requested", contract_version=1,
            source_domain="cards_payments", source_identity=payment_id,
            destination_domain="record_to_report",
            occurrence_key="customer-settlement-entry",
            correlation_id=upstream.correlation_id,
            causation_id=upstream.message_id, produced_at=produced_at,
            payload={
                "payment_id": payment_id, "journal_id": journal_id,
                "amount": amount, "currency": currency,
            },
        )
        if existing is not None:
            if existing != message:
                raise ValueError("durable settled-payment egress conflicts with source facts")
            results.append(existing)
            continue
        if max_new_messages is not None and max_new_messages == 0:
            break
        service.publish(message, recovery_job_id=recovery_job_id)
        if max_new_messages is not None:
            max_new_messages -= 1
        results.append(message)
    return tuple(results)
