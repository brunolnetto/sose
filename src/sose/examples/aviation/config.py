from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class AviationConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 713

    tail_number: str = "N-SOSE"
    leg1_number: str = "SOSE101"
    leg2_number: str = "SOSE102"
    flight_crew_capacity: int = Field(default=1, ge=1)
    inspection_team_capacity: int = Field(default=1, ge=1)
    maintenance_bay_capacity: int = Field(default=1, ge=1)
    maintenance_queue_capacity: int = Field(default=100, ge=1)
    part_lot_capacity: int = Field(default=100, ge=1)
    leg1_departure_delay: timedelta = Field(default=timedelta(hours=1), gt=timedelta(0))
    leg2_departure_delay: timedelta = Field(default=timedelta(hours=5), gt=timedelta(0))
    auto_land: bool = True
    inspection_fail: bool = False
