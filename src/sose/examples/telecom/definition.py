from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import TelecomConfig
from .simulation import (
    build_runtime,
    reconcile_activation,
    schedule_activation,
    seed_reference,
)

def _build(persistence: Persistence, config: TelecomConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: TelecomConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        customer_id=config.customer_id,
        product_offering=config.product_offering,
        access_technology=config.access_technology,
        sim_type=config.sim_type,
        provisioning_capacity=config.provisioning_capacity,
    )

def _reconcile_tick(persistence, engine, backend, config, entities):
    order = persistence.entity(
        "telecom_product_order",
        entities.product_order_id,
    )
    if order is None:
        raise RuntimeError("configured product order was not persisted")
    if order.state == "completed":
        return

    schedule_activation(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=config.activation_delay,
    )
    reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )


definition = DomainDefinition(
    name="telecom",
    description="Telecommunications fulfillment and assurance reference domain.",
    config_model=TelecomConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","activation_delay"]),
)
