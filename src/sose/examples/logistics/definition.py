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

def _shipment_or_error(persistence, entities):
    shipment = persistence.entity("shipment", entities.shipment_id)
    if shipment is None:
        raise RuntimeError("configured shipment was not persisted")
    return shipment


def _reconcile_pickup_stage(persistence, engine, backend, *, entities, shipment):
    if shipment.state not in {"pickup_scheduled", "delayed_pickup"}:
        return shipment
    reconcile_pickup(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    return persistence.entity("shipment", entities.shipment_id)


def _reconcile_origin_stage(persistence, engine, backend, *, entities, shipment):
    if shipment is None or shipment.state != "picked_up":
        return shipment
    reconcile_origin_hub(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    return persistence.entity("shipment", entities.shipment_id)


def _reconcile_transfer_stage(persistence, engine, backend, *, entities, shipment):
    if shipment is None or shipment.state not in {"at_origin_hub", "in_transfer"}:
        return shipment
    reconcile_transfer(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    return persistence.entity("shipment", entities.shipment_id)


def _reconcile_dispatch_stage(persistence, engine, backend, *, entities, shipment):
    if shipment is None or shipment.state != "at_destination_hub":
        return shipment
    reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    )
    return persistence.entity("shipment", entities.shipment_id)


def _reconcile_delivery_stage(persistence, engine, backend, *, entities, shipment):
    if shipment is None or shipment.state != "out_for_delivery":
        return
    reconcile_delivery_success(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    )


def _reconcile_tick(persistence, engine, backend, config, entities):
    if not config.auto_progress_shipment:
        return
    shipment = _shipment_or_error(persistence, entities)
    shipment = _reconcile_pickup_stage(
        persistence,
        engine,
        backend,
        entities=entities,
        shipment=shipment,
    )
    shipment = _reconcile_origin_stage(
        persistence,
        engine,
        backend,
        entities=entities,
        shipment=shipment,
    )
    shipment = _reconcile_transfer_stage(
        persistence,
        engine,
        backend,
        entities=entities,
        shipment=shipment,
    )
    shipment = _reconcile_dispatch_stage(
        persistence,
        engine,
        backend,
        entities=entities,
        shipment=shipment,
    )
    _reconcile_delivery_stage(
        persistence,
        engine,
        backend,
        entities=entities,
        shipment=shipment,
    )


definition = DomainDefinition(
    name="logistics",
    description="Logistics and transport reference domain.",
    config_model=LogisticsConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_progress_shipment"]),
)
