"""Credit-loan constants, ids, runtime wiring and entity helpers (split from simulation.py)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .scenarios import ORIGIN
from .statecharts import (
    CollectionCaseChart,
    CreditDecisionChart,
    DelinquencyCaseChart,
    InstallmentChart,
    LoanApplicationChart,
    LoanChart,
    PaymentChart,
    RestructureChart,
)

FIRST_DUE_DELAY = timedelta(hours=2)
INSTALLMENT_INTERVAL = timedelta(hours=2)
OVERDUE_GRACE = timedelta(hours=1)
COLLECTION_FOLLOWUP = timedelta(hours=1)


def _usd_cents(value: float) -> int:
    scaled = float(value) * 100
    if not math.isfinite(scaled):
        raise ValueError("USD amount must be finite")

    # Float-facing References tolerate representational noise around an exact
    # minor-unit boundary, but never enough to reinterpret a true half-minor
    # value as integral. This avoids an arbitrary magnitude cutoff while keeping
    # the acceptance region strictly narrower than half a minor unit.
    ulp = math.ulp(scaled)
    nearest = round(scaled)
    half_minor_cap = math.nextafter(0.5, 0.0)
    tolerance = min(max(1e-7, 2 * ulp), half_minor_cap)
    if abs(scaled - nearest) > tolerance:
        raise ValueError("USD amount must not contain fractional cents")
    return int(nearest)


def flow_correlation_id(application_id: str) -> str:
    return deterministic_id("credit-loans-flow", application_id)


def credit_decision_id(application_id: str) -> str:
    return deterministic_id(
        "entity", "credit_decision", "credit-loans-reference", application_id, "decision"
    )


def loan_id(application_id: str) -> str:
    return deterministic_id(
        "entity", "loan", "credit-loans-reference", application_id, "loan"
    )


def installment_id(loan_id_value: str, ordinal: int) -> str:
    return deterministic_id(
        "entity", "loan_installment", "credit-loans-reference", loan_id_value, "installment", ordinal
    )


def payment_id(installment_id_value: str, ordinal: int) -> str:
    return deterministic_id(
        "entity", "loan_payment", "credit-loans-reference", installment_id_value, "payment", ordinal
    )


def delinquency_case_id(loan_id_value: str, installment_id_value: str) -> str:
    return deterministic_id(
        "entity",
        "delinquency_case",
        "credit-loans-reference",
        loan_id_value,
        installment_id_value,
        "delinquency",
    )


def collection_case_id(delinquency_id: str) -> str:
    return deterministic_id(
        "entity", "loan_collection_case", "credit-loans-reference", delinquency_id, "collection"
    )


def restructure_id(loan_id_value: str, ordinal: int = 1) -> str:
    return deterministic_id(
        "entity", "loan_restructure", "credit-loans-reference", loan_id_value, "restructure", ordinal
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 842,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("loan_application", LoanApplicationChart))
    registry.register(EntityType("credit_decision", CreditDecisionChart))
    registry.register(EntityType("loan", LoanChart))
    registry.register(EntityType("loan_installment", InstallmentChart))
    registry.register(EntityType("loan_payment", PaymentChart))
    registry.register(EntityType("delinquency_case", DelinquencyCaseChart))
    registry.register(EntityType("loan_collection_case", CollectionCaseChart))
    registry.register(EntityType("loan_restructure", RestructureChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def _entity(persistence, entity_type, entity_id):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _dispatch(engine, entity, event, *, key, correlation_id):
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)
