from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Payment(Entity):
    entity_type: str = "card_payment"


@dataclass(slots=True)
class Dispute(Entity):
    entity_type: str = "payment_dispute"
