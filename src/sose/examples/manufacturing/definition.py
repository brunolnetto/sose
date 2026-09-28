from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import ManufacturingConfig
from .simulation import build_runtime, seed_happy_path

def _build(persistence: Persistence, config: ManufacturingConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: ManufacturingConfig):
    return seed_happy_path(persistence, quantity=config.quantity)

definition = DomainDefinition(
    name="manufacturing",
    description="Manufacturing production-flow reference domain.",
    config_model=ManufacturingConfig,
    build_runtime=_build,
    seed=_seed,
)
