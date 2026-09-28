from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import LogisticsConfig
from .simulation import (
    build_runtime,
    reconcile_delivery_dispatch,
    reconcile_delivery_success,
    reconcile_origin_hub,
    reconcile_pickup,
    reconcile_transfer,
    seed_reference,
)

def _build(persistence: Persistence, config: LogisticsConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: LogisticsConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        service_level=config.service_level,
        route=config.route,
        resource_capacity=config.resource_capacity,
        hub_queue_capacity=config.hub_queue_capacity,
        pickup_delay=config.pickup_delay,
    )

def _reconcile_tick(persistence, engine, backend, config, entities):
    shipment = persistence.entity("shipment", entities.shipment_id)
    if shipment is None:
        raise RuntimeError("configured shipment was not persisted")

    if shipment.state in {"pickup_scheduled", "delayed_pickup"}:
        reconcile_pickup(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        shipment = persistence.entity("shipment", entities.shipment_id)

    if shipment is not None and shipment.state == "picked_up":
        reconcile_origin_hub(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        shipment = persistence.entity("shipment", entities.shipment_id)

    if shipment is not None and shipment.state in {"at_origin_hub", "in_transfer"}:
        reconcile_transfer(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        shipment = persistence.entity("shipment", entities.shipment_id)

    if shipment is not None and shipment.state == "at_destination_hub":
        reconcile_delivery_dispatch(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
        )
        shipment = persistence.entity("shipment", entities.shipment_id)

    if shipment is not None and shipment.state == "out_for_delivery":
        reconcile_delivery_success(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
        )


definition = DomainDefinition(
    name="logistics",
    description="Logistics and transport reference domain.",
    config_model=LogisticsConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
)
