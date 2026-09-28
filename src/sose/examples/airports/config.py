from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class AirportsConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 612
    flight_number: str = "SOSE101"
    departure_priority: int = Field(default=50, ge=0)
    gate: str = "G1"
    gate_capacity: int = Field(default=1, ge=1)
    ground_team_capacity: int = Field(default=1, ge=1)
    tug_capacity: int = Field(default=1, ge=1)
    departure_queue_capacity: int = Field(default=100, ge=1)
    arrival_delay: timedelta = Field(default=timedelta(hours=1), gt=timedelta(0))
    departure_slot_delay: timedelta = Field(
        default=timedelta(hours=4),
        gt=timedelta(0),
    )
