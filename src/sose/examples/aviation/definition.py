from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import AviationConfig
from .simulation import build_runtime, seed_reference

def _build(persistence: Persistence, config: AviationConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: AviationConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        tail_number=config.tail_number,
        leg1_number=config.leg1_number,
        leg2_number=config.leg2_number,
        flight_crew_capacity=config.flight_crew_capacity,
        inspection_team_capacity=config.inspection_team_capacity,
        maintenance_bay_capacity=config.maintenance_bay_capacity,
        maintenance_queue_capacity=config.maintenance_queue_capacity,
        part_lot_capacity=config.part_lot_capacity,
    )

definition = DomainDefinition(
    name="aviation",
    description="Aviation operations and preemption reference domain.",
    config_model=AviationConfig,
    build_runtime=_build,
    seed=_seed,
)
