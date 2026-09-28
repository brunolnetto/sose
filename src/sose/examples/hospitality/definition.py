from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import HospitalityConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: HospitalityConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: HospitalityConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        room_count=config.room_count,
        room_type=config.room_type,
    )

definition = DomainDefinition(
    name="hospitality",
    description="Hospitality and reservations reference domain.",
    config_model=HospitalityConfig,
    build_runtime=_build,
    seed=_seed,
)
