from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class ManufacturingConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 84
    quantity: float = Field(default=10.0, gt=0)
    auto_seed_material: bool = True
    quality_outcome: str = "pass"
