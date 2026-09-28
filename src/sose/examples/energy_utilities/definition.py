from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import EnergyUtilitiesConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: EnergyUtilitiesConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: EnergyUtilitiesConfig):
    return seed_reference(persistence)

definition = DomainDefinition(
    name="energy_utilities",
    description="Energy and utilities reference domain.",
    config_model=EnergyUtilitiesConfig,
    build_runtime=_build,
    seed=_seed,
)
