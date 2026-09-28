from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import TransitConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: TransitConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: TransitConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        vehicle_key=config.vehicle_key,
        block_id=config.block_id,
        first_trip_delay=config.first_trip_delay,
        trip_duration=config.trip_duration,
        layover=config.layover,
    )

definition = DomainDefinition(
    name="transit",
    description="Public transit and rail operations reference domain.",
    config_model=TransitConfig,
    build_runtime=_build,
    seed=_seed,
)
