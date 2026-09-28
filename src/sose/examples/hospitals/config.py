from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .runtime import ORIGIN

class HospitalsConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 252
    acuity: int = Field(default=50, ge=0, le=100)
