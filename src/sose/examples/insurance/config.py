from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class InsuranceConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 511
    severity: int = Field(default=50, ge=0, le=100)
    amount: float = Field(default=5000.0, gt=0)
    claim_type: str = "property"
    currency: str = "USD"
