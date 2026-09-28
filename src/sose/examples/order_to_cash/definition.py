from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import OrderToCashConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: OrderToCashConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: OrderToCashConfig):
    return seed_reference(persistence, amount=config.amount)

definition = DomainDefinition(
    name="order_to_cash",
    description="Order-to-Cash reference domain.",
    config_model=OrderToCashConfig,
    build_runtime=_build,
    seed=_seed,
)
