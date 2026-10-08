from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import WarehouseFulfillmentConfig
from .simulation import (
    allocate_order,
    build_runtime,
    reconcile_fulfillment_services,
    seed_reference,
)


def _build(
    persistence: Persistence,
    config: WarehouseFulfillmentConfig,
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


def _seed(persistence: Persistence, config: WarehouseFulfillmentConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        requested_quantity=config.requested_quantity,
        primary_on_hand=config.primary_on_hand,
        substitute_on_hand=config.substitute_on_hand,
        allow_substitute=config.allow_substitute,
        picker_capacity=config.picker_capacity,
        packing_station_capacity=config.packing_station_capacity,
        shipping_dock_capacity=config.shipping_dock_capacity,
    )


def _order_or_error(persistence, entities):
    order = persistence.entity(
        "warehouse_fulfillment_order",
        entities.order_id,
    )
    if order is None:
        raise RuntimeError("configured fulfillment order was not persisted")
    return order


def _reload_order(persistence, entities):
    return persistence.entity(
        "warehouse_fulfillment_order",
        entities.order_id,
    )


def _reconcile_requested(persistence, engine, *, entities, order):
    if order.state != "requested":
        return order
    if not allocate_order(persistence, engine, entities=entities):
        return None
    return _reload_order(persistence, entities)


def _reconcile_tick(persistence, engine, backend, config, entities):
    if not config.auto_progress_fulfillment:
        return
    order = _order_or_error(persistence, entities)
    if order.state == "shipped":
        return

    order = _reconcile_requested(
        persistence,
        engine,
        entities=entities,
        order=order,
    )
    if order is None:
        return

    reconcile_fulfillment_services(
        persistence,
        engine,
        backend,
        entities=entities,
        pick_duration=config.pick_duration,
        pack_duration=config.pack_duration,
        ship_duration=config.ship_duration,
    )


definition = DomainDefinition(
    name="warehouse_fulfillment",
    description="Warehouse and fulfillment reference domain.",
    config_model=WarehouseFulfillmentConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(
        [
            "tick_step",
            "random_seed",
            "auto_progress_fulfillment",
        ]
    ),
)
