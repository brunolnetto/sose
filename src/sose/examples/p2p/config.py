from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class P2PConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 42
    quantity: float = Field(default=10.0, gt=0)
