from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class CreditLoansConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 842
    principal: float = Field(default=1200.0, gt=0)
    installment_count: int = Field(default=3, ge=1)
