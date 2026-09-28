from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import TelecomConfig
from .simulation import build_runtime, seed_reference

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

definition = DomainDefinition(
    name="telecom",
    description="Telecommunications fulfillment and assurance reference domain.",
    config_model=TelecomConfig,
    build_runtime=_build,
    seed=_seed,
)
