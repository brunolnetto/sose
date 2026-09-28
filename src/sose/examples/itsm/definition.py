from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import ITSMConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: ITSMConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: ITSMConfig):
    return seed_reference(persistence, severity=config.severity)

definition = DomainDefinition(
    name="itsm",
    description="IT service management reference domain.",
    config_model=ITSMConfig,
    build_runtime=_build,
    seed=_seed,
)
