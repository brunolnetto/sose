from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import P2PConfig
from .simulation import (
    build_runtime,
    reconcile_consumption,
    reconcile_receiving_resources,
    reconcile_stocking,
    seed_happy_path,
)


def _build(
    persistence: Persistence,
    config: P2PConfig,
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


def _seed(persistence: Persistence, config: P2PConfig):
    return seed_happy_path(
        persistence,
        now=config.start_at,
        quantity=config.quantity,
    )


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    purchase_order = persistence.entity(
        "purchase_order",
        entities.purchase_order_id,
    )
    receipt = persistence.entity("receipt", entities.receipt_id)
    demand = persistence.entity(
        "material_demand",
        entities.material_demand_id,
    )
    if purchase_order is None or receipt is None or demand is None:
        raise RuntimeError("P2P reference entities were not persisted")

    # Supplier lead time is represented by the durable purchase-order schedule.
    if purchase_order.state not in {"received", "closed"}:
        return

    if receipt.state in {"pending", "receiving", "partial"}:
        reconcile_receiving_resources(
            persistence,
            engine,
            backend,
            entities=entities,
            outcome=config.receipt_outcome,
        )
        return

    if receipt.state == "inspected":
        reconcile_stocking(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=config.quantity,
        )
        return

    if (
        receipt.state == "stocked"
        and config.auto_consume_inventory
        and demand.state != "consumed"
    ):
        reconcile_consumption(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=config.quantity,
        )


definition = DomainDefinition(
    name="p2p",
    description="Procure-to-Pay reference domain.",
    config_model=P2PConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_consume_inventory","receipt_outcome"]),
)
