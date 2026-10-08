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
    picker_capacity: int = Field(default=1, ge=1)
    packing_station_capacity: int = Field(default=1, ge=1)
    shipping_dock_capacity: int = Field(default=1, ge=1)
    pick_duration: timedelta = Field(default=timedelta(hours=2), gt=timedelta(0))
    pack_duration: timedelta = Field(default=timedelta(hours=1), gt=timedelta(0))
    ship_duration: timedelta = Field(default=timedelta(hours=3), gt=timedelta(0))
    auto_progress_fulfillment: bool = True
