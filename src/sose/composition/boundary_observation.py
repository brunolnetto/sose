"""Bounded read-side publication / delivery / ACK reconciliation for #496 W3c.

Caller-selected durable IDs, not domain-only discovery, delimit observation.
The v1 BoundaryMessage lacks immutable component owner evidence: a contract
match cannot certify organizational membership, business effect, or execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sose.core.identity import deterministic_id

from .model import BoundaryConsumption, BoundaryDelivery, DeliveryStatus
from .organization import OrganizationDefinition, validate_organization_admission

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class SelectedBoundaryReceipt:
    message_id: str
    declared_producer_component_id: str
    declared_consumer_component_id: str
    contract_name: str
    contract_version: int
    correlation_id: str
    causation_id: str | None
    causation_kind: str | None
    delivery_id: str
    status: DeliveryStatus
    attempts: int
    claim_epoch: int
    claim_worker_id: str | None
    consumption_id: str | None
    consumer_effect_id: str | None


@dataclass(frozen=True, slots=True)
class SelectedBoundaryProtocolEvidence:
    organization_id: str
    definition_fingerprint: str
    records: tuple[SelectedBoundaryReceipt, ...]


def inspect_selected_boundary_protocol(
    definition: OrganizationDefinition,
    persistence: "Persistence",
    *,
    message_ids: tuple[str, ...],
    catalog,
    manifests,
    consumers,
) -> SelectedBoundaryProtocolEvidence:
    """Read selected immutable messages, delivery states and ACKs from ONE UoW.

    A selected ID is a *query filter*, never a proof that the message belongs
    to an organizational owner. The exact admitted domain-contract edge is
    required, but its source_identity is not an owner-qualified component key.
    This verifier attests consistency of *stored protocol records only*;
    a consumer effect ID is not a business-effect certificate or a Command.
    """
    fingerprint = validate_organization_admission(
        definition, catalog=catalog, manifests=manifests, consumers=consumers,
    )
    if not isinstance(message_ids, tuple) or any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in message_ids
    ) or len(message_ids) != len(set(message_ids)):
        raise ValueError("selected boundary message IDs must be unique canonical strings")
    by_component = {c.component_id: c for c in definition.components}
    admitted_edges = {}
    for edge in definition.boundaries:
        producer = by_component[edge.producer_id]
        consumer = by_component[edge.consumer_id]
        key = (producer.domain, consumer.domain, edge.contract_name, edge.contract_version)
        if key in admitted_edges:
            raise ValueError("ambiguous admitted boundary edge")
        admitted_edges[key] = edge

    selected = set(message_ids)
    # Read all three protocol families from one authoritative UoW. No global
    # snapshot of other owners' jobs, clocks or shared physical reservations.
    reader = getattr(persistence, "boundary_protocol_snapshot", None)
    if callable(reader):
        # PostgreSQL: one repeatable-read read-only snapshot. No writer
        # lease/epoch is required, even when global fencing is active.
        messages, deliveries, consumptions = reader()
    else:
        with persistence.transaction() as uow:
            messages, deliveries, consumptions = (
                uow.boundary_messages(),
                uow.boundary_deliveries(),
                uow.boundary_consumptions(),
            )

    by_message = {}
    for message in messages:
        if message.message_id in selected:
            if type(message.contract_version) is not int or message.contract_version < 1:
                raise ValueError("selected boundary publication requires integer contract version")
            identity = deterministic_id(
                "boundary-message", message.source_domain, message.source_identity,
                message.contract_name, message.contract_version,
                message.destination_domain, message.occurrence_key,
            )
            if message.message_id != identity:
                raise ValueError("selected boundary publication has forged deterministic identity")
            if message.message_id in by_message:
                raise ValueError("duplicate selected durable boundary message")
            by_message[message.message_id] = message

    missing = selected - set(by_message)
    if missing:
        raise ValueError("selected durable boundary message missing")

    by_delivery = {}
    selected_delivery_ids = set()
    selected_consumption_ids = set()
    for message in by_message.values():
        expected = BoundaryDelivery.pending(message)
        selected_delivery_ids.add(expected.delivery_id)
        selected_consumption_ids.add(deterministic_id(
            "boundary-consumption", expected.delivery_id, expected.message_id,
            expected.destination_domain, expected.contract_name,
            expected.contract_version,
        ))
    for delivery in deliveries:
        if delivery.message_id in selected or delivery.delivery_id in selected_delivery_ids:
            if type(delivery.contract_version) is not int or delivery.contract_version < 1:
                raise ValueError("selected boundary delivery requires integer contract version")
            # A second delivery with the same message_id but a forged ID
            # must not disappear merely because the correct delivery exists.
            if delivery.message_id not in by_message:
                raise ValueError("selected delivery references another durable message")
            expected = BoundaryDelivery.pending(by_message[delivery.message_id])
            if delivery.delivery_id != expected.delivery_id:
                raise ValueError("selected delivery identity contradicts publication")
            if delivery.delivery_id in by_delivery:
                raise ValueError("duplicate selected durable boundary delivery")
            by_delivery[delivery.delivery_id] = delivery

    by_receipt = {}
    for receipt in consumptions:
        if (
            receipt.message_id in selected
            or receipt.delivery_id in selected_delivery_ids
            or receipt.consumption_id in selected_consumption_ids
        ):
            if type(receipt.contract_version) is not int or receipt.contract_version < 1:
                raise ValueError("selected boundary ACK requires integer contract version")
            if receipt.delivery_id not in selected_delivery_ids:
                raise ValueError("selected ACK identity references foreign delivery")
            delivery = by_delivery.get(receipt.delivery_id)
            if delivery is None or receipt.message_id != delivery.message_id:
                raise ValueError("selected ACK contradicts durable delivery identity")
            if receipt.delivery_id in by_receipt:
                raise ValueError("duplicate selected durable boundary ACK")
            by_receipt[receipt.delivery_id] = receipt

    output = []
    for message_id in sorted(selected):
        message = by_message[message_id]
        edge = admitted_edges.get((
            message.source_domain, message.destination_domain,
            message.contract_name, message.contract_version,
        ))
        if edge is None:
            raise ValueError("selected boundary message has no exact admitted edge")
        expected = BoundaryDelivery.pending(message)
        delivery = by_delivery.get(expected.delivery_id)
        if delivery is None:
            raise ValueError("selected published message lacks durable delivery")
        if (
            delivery != expected and (
                delivery.delivery_id != expected.delivery_id
                or delivery.message_id != message.message_id
                or delivery.destination_domain != message.destination_domain
                or delivery.contract_name != message.contract_name
                or delivery.contract_version != message.contract_version
            )
        ):
            raise ValueError("selected delivery contradicts immutable publication")
        receipt = by_receipt.get(delivery.delivery_id)
        if delivery.consumer_effect_id is not None and (
            not isinstance(delivery.consumer_effect_id, str)
            or not delivery.consumer_effect_id.strip()
        ):
            raise ValueError("selected delivery has invalid consumer effect identity")
        if receipt is not None and (
            not isinstance(receipt.consumer_effect_id, str)
            or not receipt.consumer_effect_id.strip()
        ):
            raise ValueError("selected ACK has invalid consumer effect identity")
        if not isinstance(delivery.status, DeliveryStatus):
            raise ValueError("selected delivery has invalid status")
        if delivery.status is DeliveryStatus.PENDING and (
            type(delivery.attempts) is not int
            or type(delivery.claim_epoch) is not int
            or delivery.attempts != 0
            or delivery.claim_epoch != 0
        ):
            raise ValueError("pending selected delivery carries impossible claim history")
        if delivery.status in (DeliveryStatus.CLAIMED, DeliveryStatus.CONSUMED):
            if (
                type(delivery.attempts) is not int
                or type(delivery.claim_epoch) is not int
                or delivery.attempts < 1
                or delivery.claim_epoch != delivery.attempts
                or not isinstance(delivery.claim_owner_id, str)
                or not delivery.claim_owner_id.strip()
                or delivery.claimed_at is None
                or delivery.lease_expires_at is None
                # Production has independent logical source time and lease
                # wall time. They must not be compared across clock domains.
                or not (delivery.claimed_at < delivery.lease_expires_at)
            ):
                raise ValueError("selected delivery lacks original claim provenance")
        if delivery.status is DeliveryStatus.CONSUMED:
            if (
                delivery.consumed_at is None
                or not (delivery.claimed_at <= delivery.consumed_at
                        < delivery.lease_expires_at)
            ):
                raise ValueError("consumed selected delivery violates claim time fence")
            if receipt is None:
                raise ValueError("consumed selected delivery lacks durable ACK")
            expected_receipt = BoundaryConsumption.create(
                delivery=delivery, consumer_effect_id=delivery.consumer_effect_id,
                consumed_at=delivery.consumed_at,
            )
            if receipt != expected_receipt:
                raise ValueError("durable selected ACK contradicts delivery")
        elif receipt is not None:
            raise ValueError("unconsumed selected delivery unexpectedly has ACK")
        output.append(SelectedBoundaryReceipt(
            message_id=message_id,
            declared_producer_component_id=edge.producer_id,
            declared_consumer_component_id=edge.consumer_id,
            contract_name=message.contract_name,
            contract_version=message.contract_version,
            correlation_id=message.correlation_id,
            causation_id=message.causation_id,
            causation_kind=message.causation_kind,
            delivery_id=delivery.delivery_id,
            status=delivery.status,
            attempts=delivery.attempts,
            claim_epoch=delivery.claim_epoch,
            claim_worker_id=delivery.claim_owner_id,
            consumption_id=receipt.consumption_id if receipt else None,
            consumer_effect_id=delivery.consumer_effect_id,
        ))
    return SelectedBoundaryProtocolEvidence(
        organization_id=definition.organization_id,
        definition_fingerprint=fingerprint,
        records=tuple(output),
    )
