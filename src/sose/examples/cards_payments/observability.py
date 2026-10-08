from __future__ import annotations

from dataclasses import dataclass

from sose.persistence.base import Persistence

from .simulation import PaymentEntities, dispute_entity_id


_PAYMENT_TERMINAL_OUTCOMES = frozenset({"declined", "reversed", "refunded"})
_DISPUTE_TERMINAL_STATES = frozenset(
    {"merchant_won", "cardholder_won", "withdrawn"}
)


@dataclass(frozen=True, slots=True)
class CardsPaymentsProjection:
    """Read-only canonical projection of durable Cards & Payments truth."""

    payment_id: str
    dispute_id: str | None
    payment_state: str
    dispute_state: str | None
    amount: float
    currency: str
    settled: bool
    refunded: bool
    terminal_outcome: str | None
    dispute_open: bool
    settlement_lead_time_seconds: float | None


@dataclass(frozen=True, slots=True)
class CardsPaymentsKpis:
    """Canonical payment KPIs derived from durable state and immutable events."""

    settlement_lead_time_seconds: float | None
    amount: float
    transition_count: int
    authorization_decline_count: int
    settlement_retry_count: int
    refund_count: int
    dispute_count: int
    chargeback_count: int
    settled: bool


def _process_events(
    persistence: Persistence,
    *,
    payment_id: str,
):
    process_entity_ids = {payment_id, dispute_entity_id()}
    return tuple(
        event
        for event in persistence.events()
        if event.name == "entity.state_transition"
        and event.entity_id in process_entity_ids
    )


def cards_payments_projection(
    persistence: Persistence,
    *,
    entities: PaymentEntities,
) -> CardsPaymentsProjection:
    """Project payment/dispute truth without mutating operational state."""

    payment = persistence.entity("card_payment", entities.payment_id)
    if payment is None:
        raise RuntimeError(f"card payment was not persisted: {entities.payment_id}")

    dispute_key = dispute_entity_id()
    dispute = persistence.entity("payment_dispute", dispute_key)
    events = _process_events(persistence, payment_id=payment.id)
    settlement_events = tuple(
        event
        for event in events
        if event.entity_type == "card_payment"
        and event.payload.get("to_state") == "settled"
    )
    settlement_event = (
        min(settlement_events, key=lambda event: event.occurred_at)
        if settlement_events
        else None
    )

    settlement_lead_time_seconds = None
    if payment.created_at is not None and settlement_event is not None:
        settlement_lead_time_seconds = max(
            0.0,
            (settlement_event.occurred_at - payment.created_at).total_seconds(),
        )

    terminal_outcome = (
        payment.state if payment.state in _PAYMENT_TERMINAL_OUTCOMES else None
    )
    dispute_open = (
        dispute is not None and dispute.state not in _DISPUTE_TERMINAL_STATES
    )

    return CardsPaymentsProjection(
        payment_id=payment.id,
        dispute_id=dispute.id if dispute is not None else None,
        payment_state=payment.state,
        dispute_state=dispute.state if dispute is not None else None,
        amount=float(payment.attributes["amount"]),
        currency=str(payment.attributes["currency"]),
        settled=settlement_event is not None,
        refunded=payment.state == "refunded",
        terminal_outcome=terminal_outcome,
        dispute_open=dispute_open,
        settlement_lead_time_seconds=settlement_lead_time_seconds,
    )


def cards_payments_kpis(
    persistence: Persistence,
    *,
    entities: PaymentEntities,
) -> CardsPaymentsKpis:
    """Return stable payment KPIs from durable projection and event history."""

    projection = cards_payments_projection(persistence, entities=entities)
    events = _process_events(persistence, payment_id=entities.payment_id)

    def count(entity_type: str, to_state: str) -> int:
        return sum(
            event.entity_type == entity_type
            and event.payload.get("to_state") == to_state
            for event in events
        )

    return CardsPaymentsKpis(
        settlement_lead_time_seconds=projection.settlement_lead_time_seconds,
        amount=projection.amount,
        transition_count=len(events),
        authorization_decline_count=count("card_payment", "declined"),
        settlement_retry_count=count("card_payment", "settlement_retry_wait"),
        refund_count=count("card_payment", "refunded"),
        dispute_count=int(projection.dispute_id is not None),
        chargeback_count=count("payment_dispute", "chargeback"),
        settled=projection.settled,
    )
