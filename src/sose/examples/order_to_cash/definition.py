from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import OrderToCashConfig
from .simulation import (
    build_runtime,
    collect_receivable,
    reconcile_collection,
    receivable_id,
    reconcile_credit,
    reconcile_fulfillment,
    schedule_due,
    schedule_overdue,
    seed_reference,
    ship_invoice_and_ensure_receivable,
)


def _build(
    persistence: Persistence,
    config: OrderToCashConfig,
    now: datetime,
    tick: int,
):
    return build_runtime(
        persistence,
        now=now,
        tick=tick,
        step=config.tick_step,
        random_seed=config.random_seed,
    )


def _seed(persistence: Persistence, config: OrderToCashConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        amount=config.amount,
        currency=config.currency,
    )


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    order = persistence.entity("sales_order", entities.order_id)
    if order is None:
        raise RuntimeError("sales order was not persisted")

    if order.state in {"submitted", "credit_hold"}:
        reconcile_credit(
            persistence,
            engine,
            entities=entities,
        )
        return

    if order.state in {"ordered", "fulfilling", "partial_fulfillment"}:
        reconcile_fulfillment(
            persistence,
            engine,
            backend,
            entities=entities,
            partial=config.partial_fulfillment,
        )
        return

    if order.state in {"fulfilled", "shipped"}:
        ship_invoice_and_ensure_receivable(
            persistence,
            engine,
            entities=entities,
        )
        return

    if order.state != "invoiced":
        return

    receivable = persistence.entity(
        "receivable",
        receivable_id(entities.order_id),
    )
    if receivable is None:
        receivable = ship_invoice_and_ensure_receivable(
            persistence,
            engine,
            entities=entities,
        )

    if receivable.state == "open":
        schedule_due(
            persistence,
            engine,
            backend,
            entities=entities,
            delay=config.due_delay,
        )
        return

    if receivable.state == "due":
        if config.auto_collect:
            collect_receivable(
                persistence,
                engine,
                entities=entities,
            )
        else:
            schedule_overdue(
                persistence,
                engine,
                backend,
                entities=entities,
                delay=config.overdue_delay,
            )
        return

    if receivable.state == "overdue":
        if config.auto_collect:
            collect_receivable(
                persistence,
                engine,
                entities=entities,
            )
        else:
            reconcile_collection(
                persistence,
                engine,
                backend,
                entities=entities,
                promise=config.collection_promise,
            )


definition = DomainDefinition(
    name="order_to_cash",
    description="Order-to-Cash reference domain.",
    config_model=OrderToCashConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
)
