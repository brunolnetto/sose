from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import ConstructionConfig
from .runtime import build_runtime, seed_reference

def _build(persistence: Persistence, config: ConstructionConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: ConstructionConfig):
    return seed_reference(persistence, quantity=config.quantity, predecessor_completed=config.predecessor_completed)

definition = DomainDefinition(
    name="construction",
    description="Construction planning and execution reference domain.",
    config_model=ConstructionConfig,
    build_runtime=_build,
    seed=_seed,
)
