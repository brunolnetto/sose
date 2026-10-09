"""Durable, immutable evidence of an applied PC6 business effect.

This is deliberately distinct from BoundaryConsumption: an ACK records a
transport acceptance, not that a domain transition committed. Evidence
is finalized transactionally with staged-Command deletion and can be
audited/replayed independently of the runtime's ephemeral SimPy backend.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sose.core.identity import deterministic_id
from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class BusinessEffectApplied:
    receipt_id: str
    effect_id: str
    boundary_message_id: str
    correlation_id: str
    entity_type: str
    entity_id: str
    terminal_state: str
    entity_version: int
    completed_at: datetime

    def __post_init__(self) -> None:
        if not all((
            self.receipt_id, self.effect_id, self.boundary_message_id,
            self.correlation_id, self.entity_type, self.entity_id,
            self.terminal_state,
        )):
            raise ValueError("business-effect certificate requires all identities")
        if self.entity_version < 0:
            raise ValueError("business-effect certificate version must be non-negative")

    @classmethod
    def from_applied(
        cls, *, effect_id: str, boundary_message_id: str,
        correlation_id: str, entity_type: str, entity_id: str,
        terminal_state: str, entity_version: int, completed_at: datetime,
    ) -> "BusinessEffectApplied":
        return cls(
            receipt_id=deterministic_id("business-effect-applied", effect_id),
            effect_id=effect_id, boundary_message_id=boundary_message_id,
            correlation_id=correlation_id, entity_type=entity_type,
            entity_id=entity_id, terminal_state=terminal_state,
            entity_version=entity_version, completed_at=completed_at,
        )


# Restrict certification to transitions with a concrete reference-model
# terminal outcome. Other legacy commands retain the v1 completion convention
# until their own business-result semantics are specified.
_CERTIFIED_TERMINALS = {
    "composition.deliver_shipment": ("shipment", "delivered"),
    "composition.complete_external_fulfillment": ("sales_order", "invoiced"),
    "composition.settle_customer_payment": ("card_payment", "settled"),
    "composition.post_customer_journal": ("journal_entry", "posted"),
    "composition.post_replenishment_journal": ("journal_entry", "posted"),
}


class BusinessEffectService:
    def __init__(self, persistence: Persistence):
        self.persistence = persistence

    def get(self, effect_id: str) -> BusinessEffectApplied | None:
        with self.persistence.transaction() as uow:
            return uow.get_business_effect(effect_id)

    def complete(
        self, *, effect_id: str, completed_at: datetime,
    ) -> BusinessEffectApplied:
        """Atomically certify committed domain truth and retire its Command.

        No certificate is created for an ACK alone. Every predicate, certificate
        insertion and Command deletion runs inside the same authoritative UoW.
        Repeating the same effect ID returns immutable prior evidence.
        """
        with self.persistence.transaction() as uow:
            prior = uow.get_business_effect(effect_id)
            pending = uow.get_command(effect_id)
            if prior is not None:
                if pending is not None:
                    raise ValueError("certified effect conflicts with a pending Command")
                return prior
            if pending is None:
                raise ValueError("business effect lacks pending Command and certificate")
            expected = _CERTIFIED_TERMINALS.get(pending.name)
            if expected is None:
                raise ValueError(f"business effect has no certified terminal: {pending.name}")
            expected_type, expected_state = expected
            if pending.entity_type != expected_type:
                raise ValueError("business effect target entity type conflicts with command")
            if not pending.causation_id or not pending.correlation_id:
                raise ValueError("business effect lacks causal Command identity")

            matching = []
            for delivery in uow.boundary_deliveries():
                if delivery.message_id != pending.causation_id:
                    continue
                consumed = uow.get_boundary_consumption(delivery.delivery_id)
                if (
                    consumed is not None
                    and consumed.consumer_effect_id == effect_id
                    and delivery.consumer_effect_id == effect_id
                    and delivery.status.value == "consumed"
                ):
                    matching.append(delivery)
            if len(matching) != 1:
                raise ValueError("business effect lacks unique accepted boundary receipt")

            message = uow.get_boundary_message(pending.causation_id)
            if message is None or message.correlation_id != pending.correlation_id:
                raise ValueError("business effect causation contradicts durable source message")
            entity = uow.get_entity(pending.entity_type, pending.entity_id)
            if entity is None or entity.state != expected_state:
                raise ValueError(f"business effect lacks terminal state {expected_state}")

            if pending.name == "composition.complete_external_fulfillment":
                # Invoicing alone is not a collectable payment request.
                from sose.examples.order_to_cash.simulation import receivable_id
                receivable = uow.get_entity("receivable", receivable_id(pending.entity_id))
                if (
                    receivable is None or receivable.state != "open"
                    or receivable.attributes.get("order_id") != pending.entity_id
                    or receivable.attributes.get("amount") != entity.attributes.get("amount")
                    or receivable.attributes.get("currency") != entity.attributes.get("currency")
                ):
                    raise ValueError("invoiced effect lacks consistent open receivable")

            certificate = BusinessEffectApplied.from_applied(
                effect_id=effect_id,
                boundary_message_id=message.message_id,
                correlation_id=message.correlation_id,
                entity_type=entity.entity_type,
                entity_id=entity.id,
                terminal_state=entity.state,
                entity_version=entity.version,
                completed_at=completed_at,
            )
            uow.save_business_effect(certificate)
            uow.delete_command(effect_id)
            return certificate


CERTIFIED_INTENTS = frozenset(_CERTIFIED_TERMINALS)
