from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import CardsPaymentsConfig
from .simulation import (
    build_runtime,
    reconcile_authorization,
    reconcile_capture_and_schedule_settlement,
    reconcile_settlement,
    seed_reference,
)

def _build(persistence: Persistence, config: CardsPaymentsConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: CardsPaymentsConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        amount=config.amount,
        currency=config.currency,
        processor_capacity=config.processor_capacity,
    )

def _reconcile_tick(persistence, engine, backend, config, entities):
    payment = persistence.entity("card_payment", entities.payment_id)
    if payment is None:
        raise RuntimeError("configured payment was not persisted")

    if payment.state == "authorization_requested":
        reconcile_authorization(
            persistence,
            engine,
            backend,
            entities=entities,
            outcome="authorize",
        )
        payment = persistence.entity("card_payment", entities.payment_id)

    if payment is not None and payment.state == "authorized":
        reconcile_capture_and_schedule_settlement(
            persistence,
            engine,
            backend,
            entities=entities,
            delay=config.settlement_delay,
        )
        return

    if payment is not None and payment.state == "settlement_pending":
        reconcile_settlement(
            persistence,
            engine,
            backend,
            entities=entities,
            outcome="success",
        )


definition = DomainDefinition(
    name="cards_payments",
    description="Cards and payments reference domain.",
    config_model=CardsPaymentsConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
)
