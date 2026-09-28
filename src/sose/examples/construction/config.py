from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .runtime import ORIGIN

class ConstructionConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 294
    quantity: float = Field(default=10.0, gt=0)
    predecessor_completed: bool = True
