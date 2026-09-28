from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import Dispute, Payment
from .scenarios import ORIGIN
from .statecharts import DisputeChart, PaymentChart


SETTLEMENT_DELAY = timedelta(hours=2)
RETRY_DELAY = timedelta(hours=2)
EVIDENCE_DELAY = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class PaymentEntities:
    payment_id: str


def flow_correlation_id() -> str:
    return deterministic_id("cards-payment-flow", "reference", "payment-1")


def dispute_entity_id() -> str:
    return deterministic_id(
        "entity",
        "payment_dispute",
        "cards-reference",
        "payment-1",
        "dispute-1",
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 168,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("card_payment", PaymentChart))
    registry.register(EntityType("payment_dispute", DisputeChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    amount: float = 125.0,
    currency: str = "USD",
    processor_capacity: int = 1,
) -> PaymentEntities:
    context, _ = build_runtime(persistence, now=now)
    payment = context.entities.create(
        Payment,
        key=("cards-reference", "payment-1"),
        state="authorization_requested",
        attributes={"amount": amount, "currency": currency},
    )
    with persistence.transaction() as uow:
        uow.save_entity(payment)
        for name in (
            "authorization_processor",
            "settlement_processor",
            "dispute_analyst",
        ):
            uow.save_resource_definition(
                ResourceDefinition(name, capacity=processor_capacity)
            )
    return PaymentEntities(payment_id=payment.id)


def _payment(persistence: MemoryPersistence, entities: PaymentEntities) -> Payment:
    payment = persistence.entity("card_payment", entities.payment_id)
    if payment is None:
        raise RuntimeError("payment was not persisted")
    return payment


def _dispute(persistence: MemoryPersistence) -> Dispute | None:
    return persistence.entity("payment_dispute", dispute_entity_id())


def _dispatch(engine: Engine, entity, event: str, *, key: tuple[object, ...]) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=flow_correlation_id(),
        key=key,
    )
    engine.dispatch(command)


def _processor_available(engine: Engine) -> bool:
    return bool(
        engine.context.scenarios.attribute("payments.processor.available", True)
    )


def reconcile_authorization(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: PaymentEntities,
    outcome: str = "authorize",
) -> bool:
    if outcome not in {"authorize", "decline"}:
        raise ValueError(f"unsupported authorization outcome: {outcome}")

    payment = _payment(persistence, entities)
    if payment.state != "authorization_requested":
        return payment.state in {"authorized", "declined"}

    if not _processor_available(engine):
        return False

    request_id = f"authorization-processor:{payment.id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="authorization_processor",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    payment = _payment(persistence, entities)
    _dispatch(
        engine,
        payment,
        outcome,
        key=("cards-authorization", payment.id, outcome),
    )
    engine.resources.withdraw(backend, request_id)
    return True


def reconcile_reversal(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: PaymentEntities,
) -> bool:
    payment = _payment(persistence, entities)
    if payment.state == "reversed":
        return True
    if payment.state != "authorized":
        return False

    _dispatch(
        engine,
        payment,
        "reverse",
        key=("cards-reversal", payment.id),
    )
    return True


def reconcile_capture_and_schedule_settlement(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: PaymentEntities,
    delay: timedelta = SETTLEMENT_DELAY,
) -> datetime:
    payment = _payment(persistence, entities)
    if payment.state != "authorized":
        raise RuntimeError(f"payment is not capturable: {payment.state}")

    _dispatch(
        engine,
        payment,
        "capture",
        key=("cards-capture", payment.id),
    )
    payment = _payment(persistence, entities)
    due_at = backend.now + delay
    command = engine.context.commands.create(
        "settlement_due",
        target=payment,
        due_at=due_at,
        correlation_id=flow_correlation_id(),
        key=("cards-settlement", payment.id, "due"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def reconcile_settlement(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: PaymentEntities,
    outcome: str = "success",
    retry_after: timedelta = RETRY_DELAY,
) -> datetime | None:
    if outcome not in {"success", "retry"}:
        raise ValueError(f"unsupported settlement outcome: {outcome}")

    payment = _payment(persistence, entities)
    if payment.state == "settled":
        return None
    if payment.state != "settlement_pending":
        return None

    if not _processor_available(engine):
        return None

    request_id = f"settlement-processor:{payment.id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="settlement_processor",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return None

    payment = _payment(persistence, entities)
    if outcome == "success":
        _dispatch(
            engine,
            payment,
            "settle",
            key=("cards-settlement", payment.id, "settle"),
        )
        engine.resources.withdraw(backend, request_id)
        return None

    _dispatch(
        engine,
        payment,
        "wait_retry",
        key=("cards-settlement", payment.id, "wait-retry"),
    )
    payment = _payment(persistence, entities)
    due_at = backend.now + retry_after
    command = engine.context.commands.create(
        "retry_settlement",
        target=payment,
        due_at=due_at,
        correlation_id=flow_correlation_id(),
        key=("cards-settlement", payment.id, "retry"),
    )
    engine.context.schedules.at(due_at, command=command)
    engine.resources.withdraw(backend, request_id)
    return due_at


def reconcile_refund(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: PaymentEntities,
) -> bool:
    payment = _payment(persistence, entities)
    if payment.state == "refunded":
        return True
    if payment.state != "settled" or not _processor_available(engine):
        return False

    request_id = f"refund-processor:{payment.id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="settlement_processor",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    payment = _payment(persistence, entities)
    _dispatch(
        engine,
        payment,
        "refund",
        key=("cards-refund", payment.id),
    )
    engine.resources.withdraw(backend, request_id)
    return True


def ensure_dispute(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: PaymentEntities,
) -> Dispute:
    existing = _dispute(persistence)
    if existing is not None:
        return existing

    payment = _payment(persistence, entities)
    if payment.state != "settled":
        raise RuntimeError("dispute requires a settled payment")

    dispute = engine.context.entities.create(
        Dispute,
        key=("cards-reference", "payment-1", "dispute-1"),
        state="opened",
        attributes={"payment_id": payment.id},
    )
    with persistence.transaction() as uow:
        uow.save_entity(dispute)
    return dispute


def start_dispute(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: PaymentEntities,
    evidence_after: timedelta = EVIDENCE_DELAY,
) -> datetime:
    dispute = ensure_dispute(persistence, engine, entities=entities)
    if dispute.state != "opened":
        raise RuntimeError(f"dispute is not open: {dispute.state}")

    _dispatch(
        engine,
        dispute,
        "request_evidence",
        key=("cards-dispute", dispute.id, "request-evidence"),
    )
    dispute = _dispute(persistence)
    if dispute is None:
        raise RuntimeError("dispute disappeared")
    due_at = backend.now + evidence_after
    command = engine.context.commands.create(
        "submit_evidence",
        target=dispute,
        due_at=due_at,
        correlation_id=flow_correlation_id(),
        key=("cards-dispute", dispute.id, "submit-evidence"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def reconcile_dispute(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    outcome: str = "merchant",
) -> bool:
    if outcome not in {"merchant", "cardholder"}:
        raise ValueError(f"unsupported dispute outcome: {outcome}")

    dispute = _dispute(persistence)
    if dispute is None or dispute.state != "under_review":
        return False

    request_id = f"dispute-analyst:{dispute.id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="dispute_analyst",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    dispute = _dispute(persistence)
    if dispute is None:
        raise RuntimeError("dispute disappeared")
    _dispatch(
        engine,
        dispute,
        "issue_chargeback",
        key=("cards-dispute", dispute.id, "chargeback"),
    )
    dispute = _dispute(persistence)
    if dispute is None:
        raise RuntimeError("dispute disappeared")
    event = "resolve_merchant" if outcome == "merchant" else "resolve_cardholder"
    _dispatch(
        engine,
        dispute,
        event,
        key=("cards-dispute", dispute.id, event),
    )
    engine.resources.withdraw(backend, request_id)
    return True


def _prepare_authorized(
    persistence: MemoryPersistence,
) -> tuple[PaymentEntities, Engine, SimPyBackend]:
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    if not reconcile_authorization(
        persistence, engine, backend, entities=entities, outcome="authorize"
    ):
        raise RuntimeError("authorization capacity unavailable")
    return entities, engine, backend


def run_happy_path() -> tuple[MemoryPersistence, PaymentEntities]:
    persistence = MemoryPersistence()
    entities, engine, backend = _prepare_authorized(persistence)
    settlement_at = reconcile_capture_and_schedule_settlement(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(settlement_at)
    reconcile_settlement(
        persistence, engine, backend, entities=entities, outcome="success"
    )
    return persistence, entities


def run_retry_path() -> tuple[MemoryPersistence, PaymentEntities]:
    persistence = MemoryPersistence()
    entities, engine, backend = _prepare_authorized(persistence)
    settlement_at = reconcile_capture_and_schedule_settlement(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(settlement_at)
    retry_at = reconcile_settlement(
        persistence, engine, backend, entities=entities, outcome="retry"
    )
    if retry_at is None:
        raise RuntimeError("settlement retry was not scheduled")
    backend.run_until(retry_at)
    reconcile_settlement(
        persistence, engine, backend, entities=entities, outcome="success"
    )
    return persistence, entities


def run_refund_path() -> tuple[MemoryPersistence, PaymentEntities]:
    persistence, entities = run_happy_path()
    position = persistence.simulation_position()
    now = position.logical_time if position is not None else ORIGIN
    tick = position.logical_tick if position is not None else 0
    _, engine = build_runtime(persistence, now=now, tick=tick)
    backend = SimPyBackend(origin=now)
    engine.rebuild_backend(backend)
    if not reconcile_refund(persistence, engine, backend, entities=entities):
        raise RuntimeError("refund capacity unavailable")
    return persistence, entities


def run_dispute_path(
    *,
    outcome: str = "merchant",
) -> tuple[MemoryPersistence, PaymentEntities]:
    persistence, entities = run_happy_path()
    position = persistence.simulation_position()
    now = position.logical_time if position is not None else ORIGIN
    tick = position.logical_tick if position is not None else 0
    _, engine = build_runtime(persistence, now=now, tick=tick)
    backend = SimPyBackend(origin=now)
    engine.rebuild_backend(backend)
    evidence_at = start_dispute(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(evidence_at)
    if not reconcile_dispute(
        persistence, engine, backend, outcome=outcome
    ):
        raise RuntimeError("dispute analyst unavailable")
    return persistence, entities
