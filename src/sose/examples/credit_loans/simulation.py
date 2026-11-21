from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sose.core.runtime import ResourceDefinition
from sose.persistence.memory import MemoryPersistence

from .entities import LoanApplication
from .scenarios import ORIGIN
from .simulation_runtime import (
    COLLECTION_FOLLOWUP,
    _usd_cents,
    flow_correlation_id,
    loan_id,
    installment_id,
    build_runtime,
)
from .simulation_origination import reconcile_underwriting, disburse_and_schedule, schedule_overdue
from .simulation_payments import post_payment
from .simulation_delinquency import reconcile_collection, cure_delinquency

__all__ = [
    "COLLECTION_FOLLOWUP", "CreditLoanEntities", "ORIGIN", "build_runtime", "cure_delinquency",
    "disburse_and_schedule", "flow_correlation_id", "installment_id", "loan_id", "post_payment",
    "reconcile_collection", "reconcile_underwriting", "schedule_overdue", "seed_reference",
]


@dataclass(frozen=True, slots=True)
class CreditLoanEntities:
    application_id: str


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    principal: float = 1200.0,
    installment_count: int = 3,
) -> CreditLoanEntities:
    principal_cents = _usd_cents(principal)
    if principal_cents <= 0:
        raise ValueError("principal must be positive")
    if installment_count <= 0:
        raise ValueError("installment_count must be positive")
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
    application = context.entities.create(
        LoanApplication,
        key=("credit-loans-reference", "application-1"),
        state="submitted",
        attributes={
            "principal": principal_cents / 100,
            "currency": "USD",
            "installment_count": installment_count,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(application)
        uow.save_resource_definition(ResourceDefinition("credit_analyst", capacity=1))
        uow.save_resource_definition(ResourceDefinition("collection_agent", capacity=1))
    return CreditLoanEntities(application_id=application.id)
