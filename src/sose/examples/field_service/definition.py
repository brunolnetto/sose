from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import FieldServiceConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: FieldServiceConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: FieldServiceConfig):
    return seed_reference(persistence)

definition = DomainDefinition(
    name="field_service",
    description="Field service and workforce reference domain.",
    config_model=FieldServiceConfig,
    build_runtime=_build,
    seed=_seed,
)
