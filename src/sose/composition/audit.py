"""Audit the *durable causal graph*, not the worker's observed call order.

Only explicitly typed predecessors are treated as authoritative edges.
Frozen v1 untyped causation IDs remain opaque, even if their text happens to
equal a known message ID. The normalized fingerprint ignores physical insert
order and volatile lease epochs but retains business identity, payload hashes
and applied-effect certificates.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import TYPE_CHECKING

from .model import DeliveryStatus

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


class CausalAuditError(RuntimeError):
    """The persisted causal graph violates a durable provenance invariant."""


@dataclass(frozen=True, slots=True)
class CausalAuditReport:
    message_count: int
    typed_edges: tuple[tuple[str, str], ...]
    opaque_causes: int
    applied_effects: int
    pending_effects: int
    semantic_digest: str


_CERTIFIED_BOUNDARY_CONTRACTS = frozenset({
    "warehouse.dispatch_ready.v1",
    "logistics.delivery_completed.v1",
    "o2c.payment_requested.v1",
    "accounting.entry_requested.v1",
})


def _detect_cycle(parents: dict[str, str]) -> None:
    # A typed boundary message has zero or one direct boundary ancestor.
    visited: set[str] = set()
    for identity in sorted(parents):
        if identity in visited:
            continue
        path: set[str] = set()
        current = identity
        while current in parents and current not in visited:
            if current in path:
                raise CausalAuditError(f"causal cycle detected at {current}")
            path.add(current)
            current = parents[current]
        visited.update(path)


def audit_causal_history(persistence: Persistence) -> CausalAuditReport:
    """Validate a coherent, adapter-authoritative snapshot and its typed DAG.

    This audits persisted evidence, not a hypothetical full-state transaction
    across external side effects. Legacy v1 links are counted but not inferred
    into typed edges. The digest is semantic, not a wall-clock/lease replay hash.
    """
    tx = getattr(persistence, "boundary_transaction", None)
    context = tx() if callable(tx) else persistence.transaction()
    with context as uow:
        deliveries = uow.boundary_deliveries()
        messages = _indexed_messages(uow, deliveries)
        typed_parents, opaque = _typed_parents(messages)
        # Detect cycles first, independent of whichever temporal inconsistency
        # would also follow from the cyclic edges.
        _detect_cycle(typed_parents)
        _verify_typed_causes(uow, messages)

        # The audit must enumerate independently persisted certificates.
        # Traversing only ACK deliveries would silently miss orphaned effects.
        all_certificates = {
            proof.effect_id: proof for proof in uow.business_effects()
        }
        applied = []
        pending_count = 0
        for delivery in deliveries:
            outcome = _audit_delivery(uow, delivery, messages[delivery.message_id])
            if outcome == "pending":
                pending_count += 1
            elif outcome is not None:
                applied.append(outcome)

        reached_certificates = {entry[0] for entry in applied}
        if set(all_certificates) != reached_certificates:
            orphaned = sorted(set(all_certificates) - reached_certificates)
            raise CausalAuditError(
                f"orphaned business certificate: {orphaned[0]}"
            )
        return CausalAuditReport(
            message_count=len(messages),
            typed_edges=tuple(sorted(
                (parent, child) for child, parent in typed_parents.items()
            )),
            opaque_causes=opaque,
            applied_effects=len(applied),
            pending_effects=pending_count,
            semantic_digest=_semantic_digest(messages, applied),
        )


def _indexed_messages(uow, deliveries) -> dict:
    messages = {}
    for delivery in deliveries:
        message = uow.get_boundary_message(delivery.message_id)
        if message is None:
            raise CausalAuditError(
                f"boundary delivery has missing message {delivery.message_id}"
            )
        if (
            message.destination_domain != delivery.destination_domain
            or message.contract_name != delivery.contract_name
            or message.contract_version != delivery.contract_version
        ):
            raise CausalAuditError("boundary delivery contradicts immutable contract")
        if message.message_id in messages:
            raise CausalAuditError("duplicate durable boundary delivery for same message")
        messages[message.message_id] = message
    return messages


def _typed_parents(messages: dict) -> tuple[dict[str, str], int]:
    """Typed boundary edges (child -> parent) and the count of opaque v1 causes."""
    typed_parents: dict[str, str] = {}
    opaque = 0
    for message in messages.values():
        if message.causation_kind == "boundary":
            typed_parents[message.message_id] = message.causation_id
        elif message.causation_kind is None and message.causation_id is not None:
            opaque += 1
    return typed_parents, opaque


def _verify_typed_causes(uow, messages: dict) -> None:
    for message in messages.values():
        if message.causation_kind == "boundary":
            _verify_boundary_parent(messages, message)
        elif message.causation_kind == "event":
            _verify_event_cause(uow, message)


def _verify_boundary_parent(messages: dict, message) -> None:
    parent = messages.get(message.causation_id)
    if parent is None:
        raise CausalAuditError(
            f"missing typed boundary parent: {message.causation_id}"
        )
    if parent.correlation_id != message.correlation_id:
        raise CausalAuditError("typed boundary parent correlation mismatch")
    if parent.produced_at >= message.produced_at:
        raise CausalAuditError("typed boundary temporal inversion")


def _verify_event_cause(uow, message) -> None:
    event = uow.get_event(message.causation_id)
    if event is None:
        raise CausalAuditError(
            f"missing typed domain event: {message.causation_id}"
        )
    if event.correlation_id != message.correlation_id:
        raise CausalAuditError("typed domain event correlation mismatch")
    if event.occurred_at > message.produced_at:
        raise CausalAuditError("typed domain event temporal inversion")


def _audit_delivery(uow, delivery, message):
    """None (not consumed / uncertified), "pending", or the applied-effect row."""
    consumed = _consumed_receipt(uow, delivery)
    if consumed is None:
        return None
    effect_id = consumed.consumer_effect_id
    command = uow.get_command(effect_id)
    certificate = uow.get_business_effect(effect_id)
    if certificate is not None:
        if command is not None:
            raise CausalAuditError("certified business effect still has pending Command")
        return _verified_certificate_row(uow, certificate, effect_id, message)
    if command is not None:
        return "pending"
    if message.contract_key in _CERTIFIED_BOUNDARY_CONTRACTS:
        raise CausalAuditError(
            f"missing applied business certificate for {message.contract_key}"
        )
    return None


def _consumed_receipt(uow, delivery):
    """The ACK receipt of a consumed delivery; None while unconsumed."""
    consumed = uow.get_boundary_consumption(delivery.delivery_id)
    if delivery.status is not DeliveryStatus.CONSUMED:
        if consumed is not None:
            raise CausalAuditError("unconsumed delivery has committed ACK receipt")
        return None
    if consumed is None:
        raise CausalAuditError("consumed boundary lacks durable ACK receipt")
    if (
        consumed.message_id != delivery.message_id
        or consumed.consumer_effect_id != delivery.consumer_effect_id
        or consumed.destination_domain != delivery.destination_domain
        or consumed.contract_name != delivery.contract_name
        or consumed.contract_version != delivery.contract_version
    ):
        raise CausalAuditError("boundary ACK identity contradicts its delivery")
    return consumed


_CERTIFICATE_TARGETS = {
    "warehouse.dispatch_ready.v1": ("shipment", "shipment_id"),
    "logistics.delivery_completed.v1": ("sales_order", "order_id"),
    "o2c.payment_requested.v1": ("card_payment", "payment_id"),
    "accounting.entry_requested.v1": ("journal_entry", "journal_id"),
}


def _verified_certificate_row(uow, certificate, effect_id: str, message) -> tuple:
    if (
        certificate.effect_id != effect_id
        or certificate.boundary_message_id != message.message_id
        or certificate.correlation_id != message.correlation_id
    ):
        raise CausalAuditError("business certificate contradicts causal message")
    _verify_certified_entity(uow, certificate)
    if _certificate_target_mismatch(certificate, message):
        raise CausalAuditError("business certificate contradicts causal target identity")
    return (
        effect_id, certificate.receipt_id, certificate.boundary_message_id,
        certificate.entity_type, certificate.entity_id,
        certificate.terminal_state, certificate.entity_version,
    )


def _verify_certified_entity(uow, certificate) -> None:
    entity = uow.get_entity(certificate.entity_type, certificate.entity_id)
    if entity is None or entity.version < certificate.entity_version:
        raise CausalAuditError("business certificate lacks compatible durable entity version")
    if (
        entity.version == certificate.entity_version
        and entity.state != certificate.terminal_state
    ):
        raise CausalAuditError(
            "business certificate terminal state conflicts with entity at certified version"
        )


def _certificate_target_mismatch(certificate, message) -> bool:
    target_field = _CERTIFICATE_TARGETS.get(message.contract_key)
    return (
        target_field is None
        or certificate.entity_type != target_field[0]
        or certificate.entity_id != str(message.payload().get(target_field[1], ""))
    )


def _semantic_digest(messages: dict, applied: list) -> str:
    material = {
        "messages": sorted((
            m.message_id, m.contract_key, m.source_domain, m.source_identity,
            m.destination_domain, m.correlation_id, m.causation_kind,
            m.causation_id, m.payload_hash,
        ) for m in messages.values()),
        "applied": sorted(applied),
    }
    # Canonical JSON encoding also handles nullable typed-reference fields.
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
