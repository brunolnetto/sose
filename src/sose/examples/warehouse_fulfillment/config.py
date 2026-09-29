from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class WarehouseFulfillmentConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 1429

    requested_quantity: float = Field(default=10.0, gt=0)
    primary_on_hand: float = Field(default=6.0, ge=0)
    substitute_on_hand: float = Field(default=5.0, ge=0)
    allow_substitute: bool = True
    auto_progress_fulfillment: bool = True
