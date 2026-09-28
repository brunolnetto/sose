from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class LogisticsConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 126

    service_level: str = "standard"
    route: str = "origin-a:destination-b"
    resource_capacity: int = Field(default=1, ge=1)
    hub_queue_capacity: int = Field(default=10, ge=1)
    pickup_delay: timedelta = Field(
        default=timedelta(hours=1),
        gt=timedelta(0),
    )
