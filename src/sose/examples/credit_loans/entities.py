from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class LoanApplication(Entity):
    entity_type: str = "loan_application"


@dataclass(slots=True)
class CreditDecision(Entity):
    entity_type: str = "credit_decision"


@dataclass(slots=True)
class Loan(Entity):
    entity_type: str = "loan"


@dataclass(slots=True)
class Installment(Entity):
    entity_type: str = "loan_installment"


@dataclass(slots=True)
class Payment(Entity):
    entity_type: str = "loan_payment"


@dataclass(slots=True)
class DelinquencyCase(Entity):
    entity_type: str = "delinquency_case"


@dataclass(slots=True)
class CollectionCase(Entity):
    entity_type: str = "loan_collection_case"


@dataclass(slots=True)
class Restructure(Entity):
    entity_type: str = "loan_restructure"
