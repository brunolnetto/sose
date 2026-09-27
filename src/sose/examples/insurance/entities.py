from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Policy(Entity):
    entity_type: str = "insurance_policy"


@dataclass(slots=True)
class Claim(Entity):
    entity_type: str = "insurance_claim"


@dataclass(slots=True)
class DocumentRequest(Entity):
    entity_type: str = "insurance_document_request"


@dataclass(slots=True)
class Assessment(Entity):
    entity_type: str = "insurance_assessment"


@dataclass(slots=True)
class Reserve(Entity):
    entity_type: str = "insurance_reserve"


@dataclass(slots=True)
class Payment(Entity):
    entity_type: str = "insurance_payment"


@dataclass(slots=True)
class FraudInvestigation(Entity):
    entity_type: str = "insurance_fraud_investigation"
