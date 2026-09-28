from typing import Literal
from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class RecordToReportConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 420
    amount: float = Field(default=1000.0, gt=0)
    currency: str = "USD"
    reconciliation_outcome: Literal["match", "unmatched"] = "match"
    close_delay: timedelta = Field(default=timedelta(hours=2), gt=timedelta(0))
