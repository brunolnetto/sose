from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class TransitConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 1117

    vehicle_key: str = "vehicle-1"
    block_id: str = "block-1"
    first_trip_delay: timedelta = Field(
        default=timedelta(hours=1),
        gt=timedelta(0),
    )
    trip_duration: timedelta = Field(
        default=timedelta(hours=1),
        gt=timedelta(0),
    )
    layover: timedelta = Field(
        default=timedelta(minutes=15),
        ge=timedelta(0),
    )
    auto_reconcile_vehicle: bool = True
