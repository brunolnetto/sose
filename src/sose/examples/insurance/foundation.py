"""Insurance constants, ids, runtime/seed setup and lookups (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import Claim, Policy
from .scenarios import ORIGIN
from .statecharts import (
    AssessmentChart,
    ClaimChart,
    DocumentRequestChart,
    FraudInvestigationChart,
    PaymentChart,
    PolicyChart,
    ReserveChart,
)


DOCUMENT_DEADLINE = timedelta(hours=4)
PAYMENT_DELAY = timedelta(hours=2)


def _reference_minor_units(value: float) -> int:
    scaled = float(value) * 100
    if not math.isfinite(scaled):
        raise ValueError("Reference payout amount must be finite")

    # Float-facing References tolerate representational noise around an exact
    # minor-unit boundary, but never enough to reinterpret a true half-minor
    # value as integral. This avoids an arbitrary magnitude cutoff while keeping
    # the acceptance region strictly narrower than half a minor unit.
    ulp = math.ulp(scaled)
    nearest = round(scaled)
    half_minor_cap = math.nextafter(0.5, 0.0)
    tolerance = min(max(1e-7, 2 * ulp), half_minor_cap)
    if abs(scaled - nearest) > tolerance:
        raise ValueError("Reference payout amount must not contain fractional minor units")
    return int(nearest)


@dataclass(frozen=True, slots=True)
class InsuranceEntities:
    policy_id: str
    claim_id: str


def flow_correlation_id(claim_id: str) -> str:
    return deterministic_id("insurance-flow", claim_id)


def document_request_id(claim_id: str, ordinal: int = 1) -> str:
    return deterministic_id(
        "entity", "insurance_document_request",
        "insurance-reference", claim_id, "document-request", ordinal,
    )


def assessment_id(claim_id: str, ordinal: int = 1) -> str:
    return deterministic_id(
        "entity", "insurance_assessment",
        "insurance-reference", claim_id, "assessment", ordinal,
    )


def fraud_investigation_id(claim_id: str) -> str:
    return deterministic_id(
        "entity", "insurance_fraud_investigation",
        "insurance-reference", claim_id, "fraud-investigation",
    )


def reserve_id(claim_id: str) -> str:
    return deterministic_id(
        "entity", "insurance_reserve",
        "insurance-reference", claim_id, "reserve",
    )


def payment_id(claim_id: str) -> str:
    return deterministic_id(
        "entity", "insurance_payment",
        "insurance-reference", claim_id, "payment",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 511,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("insurance_policy", PolicyChart))
    registry.register(EntityType("insurance_claim", ClaimChart))
    registry.register(EntityType("insurance_document_request", DocumentRequestChart))
    registry.register(EntityType("insurance_assessment", AssessmentChart))
    registry.register(EntityType("insurance_reserve", ReserveChart))
    registry.register(EntityType("insurance_payment", PaymentChart))
    registry.register(
        EntityType("insurance_fraud_investigation", FraudInvestigationChart)
    )
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    severity: int = 50,
    amount: float = 5000.0,
    claim_type: str = "property",
    currency: str = "USD",
) -> InsuranceEntities:
    amount_units = _reference_minor_units(amount)
    if amount_units <= 0:
        raise ValueError("amount must be positive")
    amount = amount_units / 100
    if not currency:
        raise ValueError("currency must be non-empty")
    context, engine = build_runtime(persistence, owner_id=owner_id, now=now)
    policy = context.entities.create(
        Policy,
        key=("insurance-reference", "policy-1"),
        state="active",
        attributes={
            "coverage_limit": 10000.0,
            "currency": currency,
            "claim_type": claim_type,
        },
    )
    claim = context.entities.create(
        Claim,
        key=("insurance-reference", "claim-1"),
        state="opened",
        attributes={
            "policy_id": policy.id,
            "severity": int(severity),
            "amount": float(amount),
            "currency": currency,
            "claim_type": claim_type,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(policy)
        uow.save_entity(claim)
        uow.save_resource_definition(ResourceDefinition("claims_adjuster", capacity=1))
        uow.save_resource_definition(ResourceDefinition("fraud_investigator", capacity=1))
        uow.save_resource_definition(ResourceDefinition("payment_processor", capacity=1))
    engine.stores.define(StoreDefinition("claim_queue", kind="priority", capacity=100))
    return InsuranceEntities(policy_id=policy.id, claim_id=claim.id)


def _entity(persistence, entity_type, entity_id):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _policy(persistence, entities):
    return _entity(persistence, "insurance_policy", entities.policy_id)


def _claim(persistence, entities):
    return _entity(persistence, "insurance_claim", entities.claim_id)


def _dispatch(engine, entity, event, *, key, correlation_id):
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)
