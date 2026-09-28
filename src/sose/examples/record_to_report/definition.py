from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import RecordToReportConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: RecordToReportConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: RecordToReportConfig):
    return seed_reference(persistence, amount=config.amount, currency=config.currency)

definition = DomainDefinition(
    name="record_to_report",
    description="Record-to-Report reference domain.",
    config_model=RecordToReportConfig,
    build_runtime=_build,
    seed=_seed,
)
