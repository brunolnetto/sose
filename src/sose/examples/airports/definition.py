from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import AirportsConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: AirportsConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: AirportsConfig):
    return seed_reference(persistence, flight_number=config.flight_number, departure_priority=config.departure_priority)

definition = DomainDefinition(
    name="airports",
    description="Airport turnaround and departure operations reference domain.",
    config_model=AirportsConfig,
    build_runtime=_build,
    seed=_seed,
)
