from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import WarehouseFulfillmentConfig
from .simulation import build_runtime, seed_reference

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

definition = DomainDefinition(
    name="warehouse_fulfillment",
    description="Warehouse and fulfillment reference domain.",
    config_model=WarehouseFulfillmentConfig,
    build_runtime=_build,
    seed=_seed,
)
