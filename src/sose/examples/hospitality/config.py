from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class HospitalityConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 1319

    room_count: int = Field(default=2, ge=1, le=1000)
    room_type: str = "standard"
    arrival_after: timedelta = Field(
        default=timedelta(days=1),
        gt=timedelta(0),
    )
    stay_duration: timedelta = Field(
        default=timedelta(days=1),
        gt=timedelta(0),
    )
    hold_duration: timedelta = Field(
        default=timedelta(hours=1),
        gt=timedelta(0),
    )
    no_show_grace: timedelta = Field(
        default=timedelta(hours=2),
        gt=timedelta(0),
    )
    auto_progress_reservation: bool = True
