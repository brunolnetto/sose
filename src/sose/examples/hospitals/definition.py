from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import HospitalsConfig
from .runtime import build_runtime, seed_reference

def _build(persistence: Persistence, config: HospitalsConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: HospitalsConfig):
    return seed_reference(persistence, acuity=config.acuity)

definition = DomainDefinition(
    name="hospitals",
    description="Hospital patient-flow and procedure-capacity reference domain.",
    config_model=HospitalsConfig,
    build_runtime=_build,
    seed=_seed,
)
