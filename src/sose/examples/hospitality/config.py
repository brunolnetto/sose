from datetime import datetime, timedelta
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class HospitalityConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 1319
