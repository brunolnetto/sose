"""Immutable, authoritative business identities for Trading Company settlement.

Matching money amounts is not causal evidence: two independent orders may
share exactly the same price and currency. This record binds one order,
one card payment, and one accounting journal by explicit IDs, persists all
three keyed lookup identities, and fails closed on reassignment.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING

from sose.core.identity import deterministic_id

if TYPE_CHECKING:
    from sose.persistence.base import Persistence


@dataclass(frozen=True, slots=True)
class CustomerSettlementBinding:
    binding_id: str
    order_id: str
    payment_id: str
    journal_id: str
    amount: float
    currency: str
    correlation_id: str

    def __post_init__(self) -> None:
        if not all((
            self.binding_id, self.order_id, self.payment_id,
            self.journal_id, self.currency, self.correlation_id,
        )):
            raise ValueError("settlement binding identity cannot be empty")
        if not isinstance(self.amount, (int, float)) or not isfinite(self.amount) or self.amount <= 0:
            raise ValueError("settlement binding amount must be finite and positive")
        if self.binding_id != deterministic_id(
            "customer-settlement-binding", self.order_id,
        ):
            raise ValueError("settlement binding identity does not match its order")

    @classmethod
    def create(
        cls, *, order_id: str, payment_id: str, journal_id: str,
        amount: float, currency: str, correlation_id: str,
    ) -> "CustomerSettlementBinding":
        return cls(
            binding_id=deterministic_id("customer-settlement-binding", order_id),
            order_id=order_id,
            payment_id=payment_id,
            journal_id=journal_id,
            amount=amount, currency=currency, correlation_id=correlation_id,
        )


class SettlementBindingService:
    """Serialize immutable identity binding with all boundary writes."""

    def __init__(self, persistence: Persistence):
        self.persistence = persistence

    def _transaction(self):
        factory = getattr(self.persistence, "boundary_transaction", None)
        return factory() if callable(factory) else self.persistence.transaction()

    def bind(self, value: CustomerSettlementBinding) -> CustomerSettlementBinding:
        with self._transaction() as uow:
            for entity_type, entity_id in (
                ("sales_order", value.order_id),
                ("card_payment", value.payment_id),
                ("journal_entry", value.journal_id),
            ):
                entity = uow.get_entity(entity_type, entity_id)
                if entity is None:
                    raise ValueError(f"settlement binding missing {entity_type}: {entity_id}")
                if (
                    entity.attributes.get("amount") != value.amount
                    or entity.attributes.get("currency") != value.currency
                ):
                    raise ValueError(
                        f"settlement binding amount/currency conflicts with {entity_type}: {entity_id}"
                    )
            uow.save_customer_settlement_binding(value)
        return value

    def for_order(self, order_id: str) -> CustomerSettlementBinding | None:
        with self._transaction() as uow:
            return uow.get_customer_settlement_binding("order", order_id)

    def for_payment(self, payment_id: str) -> CustomerSettlementBinding | None:
        with self._transaction() as uow:
            return uow.get_customer_settlement_binding("payment", payment_id)

    def for_journal(self, journal_id: str) -> CustomerSettlementBinding | None:
        with self._transaction() as uow:
            return uow.get_customer_settlement_binding("journal", journal_id)
