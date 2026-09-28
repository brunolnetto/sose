from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import CreditLoansConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: CreditLoansConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: CreditLoansConfig):
    return seed_reference(persistence, principal=config.principal, installment_count=config.installment_count)

definition = DomainDefinition(
    name="credit_loans",
    description="Credit and loans reference domain.",
    config_model=CreditLoansConfig,
    build_runtime=_build,
    seed=_seed,
)
