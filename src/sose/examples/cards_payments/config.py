from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class CardsPaymentsConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 168

    amount: float = Field(default=125.0, gt=0)
    currency: str = "USD"
    processor_capacity: int = Field(default=1, ge=1)
    settlement_delay: timedelta = Field(
        default=timedelta(hours=2),
        gt=timedelta(0),
    )
