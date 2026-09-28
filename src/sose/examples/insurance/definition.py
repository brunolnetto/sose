from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import InsuranceConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: InsuranceConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: InsuranceConfig):
    return seed_reference(
        persistence,
        severity=config.severity,
        amount=config.amount,
        claim_type=config.claim_type,
        currency=config.currency,
    )

definition = DomainDefinition(
    name="insurance",
    description="Insurance claims reference domain.",
    config_model=InsuranceConfig,
    build_runtime=_build,
    seed=_seed,
)
