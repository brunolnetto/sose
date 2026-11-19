from __future__ import annotations

from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock  # noqa: F401 - historical import location
from sose.core.context import SimulationContext  # noqa: F401 - historical import location
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401 - historical import location
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler  # noqa: F401 - historical import location
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import Dispute, Payment
from .scenarios import ORIGIN
from .statecharts import DisputeChart, PaymentChart  # noqa: F401 - historical import location
from .records import (  # noqa: F401 - historical import location
    EVIDENCE_DELAY,
    RETRY_DELAY,
    SETTLEMENT_DELAY,
    PaymentEntities,
    _dispute,
    _payment,
    _processor_available,
    build_runtime,
    flow_correlation_id,
)
from .paths import (  # noqa: F401 - historical import location
    _prepare_authorized,
    run_happy_path,
)


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    amount: float = 125.0,
    currency: str = "USD",
    processor_capacity: int = 1,
    instance_key: str | None = None,
) -> PaymentEntities:
    if instance_key is not None and (not isinstance(instance_key, str) or not instance_key.strip()):
        raise ValueError("instance_key must be a nonempty string")
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)
    payment = context.entities.create(
        Payment,
        key=(("cards-reference", "payment-1") if instance_key is None
             else ("cards-reference", "payment-1", instance_key)),
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

def _dispatch(engine: Engine, entity, event: str, *, key: tuple[object, ...]) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=flow_correlation_id(),
        key=key,
    )
    engine.dispatch(command)


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
