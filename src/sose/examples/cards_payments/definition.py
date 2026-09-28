from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import CardsPaymentsConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: CardsPaymentsConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: CardsPaymentsConfig):
    return seed_reference(persistence)

definition = DomainDefinition(
    name="cards_payments",
    description="Cards and payments reference domain.",
    config_model=CardsPaymentsConfig,
    build_runtime=_build,
    seed=_seed,
)
