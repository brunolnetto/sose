from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import WarehouseFulfillmentConfig
from .simulation import (
    allocate_order,
    build_runtime,
    pack_order,
    pick_order,
    seed_reference,
    ship_order,
)

def _build(persistence: Persistence, config: WarehouseFulfillmentConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: WarehouseFulfillmentConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        requested_quantity=config.requested_quantity,
        primary_on_hand=config.primary_on_hand,
        substitute_on_hand=config.substitute_on_hand,
        allow_substitute=config.allow_substitute,
    )

def _reconcile_tick(persistence, engine, backend, config, entities):
    if not config.auto_progress_fulfillment:
        return
    order = persistence.entity(
        "warehouse_fulfillment_order",
        entities.order_id,
    )
    if order is None:
        raise RuntimeError("configured fulfillment order was not persisted")
    if order.state == "shipped":
        return

    if order.state == "requested":
        if not allocate_order(persistence, engine, entities=entities):
            return
        order = persistence.entity(
            "warehouse_fulfillment_order",
            entities.order_id,
        )

    if order is not None and order.state in {"allocated", "picking"}:
        pick_order(persistence, engine, entities=entities)
        order = persistence.entity(
            "warehouse_fulfillment_order",
            entities.order_id,
        )

    if order is not None and order.state == "picking":
        pack_order(persistence, engine, entities=entities)
        order = persistence.entity(
            "warehouse_fulfillment_order",
            entities.order_id,
        )

    if order is not None and order.state == "packed":
        ship_order(persistence, engine, entities=entities)


definition = DomainDefinition(
    name="warehouse_fulfillment",
    description="Warehouse and fulfillment reference domain.",
    config_model=WarehouseFulfillmentConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_progress_fulfillment"]),
)
