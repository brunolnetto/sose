from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: F401

from sose.backends.simpy import SimPyBackend  # noqa: F401
from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine  # noqa: F401
from sose.core.identity import deterministic_id  # noqa: F401
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import (
    CollectionCase,  # noqa: F401
    CreditDecision,  # noqa: F401
    DelinquencyCase,  # noqa: F401
    Installment,  # noqa: F401
    Loan,  # noqa: F401
    LoanApplication,
    Payment,  # noqa: F401
    Restructure,  # noqa: F401
)
from .scenarios import ORIGIN
from .statecharts import (
    CollectionCaseChart,  # noqa: F401
    CreditDecisionChart,  # noqa: F401
    DelinquencyCaseChart,  # noqa: F401
    InstallmentChart,  # noqa: F401
    LoanApplicationChart,  # noqa: F401
    LoanChart,  # noqa: F401
    PaymentChart,  # noqa: F401
    RestructureChart,  # noqa: F401
)
from .simulation_runtime import (  # noqa: F401 - re-exported, historical import location
    FIRST_DUE_DELAY, INSTALLMENT_INTERVAL, OVERDUE_GRACE, COLLECTION_FOLLOWUP, _usd_cents,
    flow_correlation_id, credit_decision_id, loan_id, installment_id, payment_id,
    delinquency_case_id, collection_case_id, restructure_id, build_runtime, _entity,
)
from .simulation_origination import (  # noqa: F401 - re-exported, historical import location
    reconcile_underwriting, ensure_loan,
    disburse_and_schedule, schedule_overdue,
)
from .simulation_payments import (  # noqa: F401 - re-exported, historical import location
    post_payment,
)
from .simulation_delinquency import (  # noqa: F401 - re-exported, historical import location
    ensure_delinquency, reconcile_collection, cure_delinquency, default_loan,
)
from .simulation_restructure import (  # noqa: F401 - re-exported, historical import location
    apply_restructure,
)


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


