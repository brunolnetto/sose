from datetime import datetime, timedelta
from pydantic import Field
from sose.domain.config import DomainConfig
from .simulation import ORIGIN

class OrderToCashConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 336
    amount: float = Field(default=250.0, gt=0)
    currency: str = "USD"
    partial_fulfillment: bool = False
    due_delay: timedelta = Field(default=timedelta(hours=2), gt=timedelta(0))
    overdue_delay: timedelta = Field(default=timedelta(hours=2), gt=timedelta(0))
    auto_collect: bool = True
    collection_promise: bool = False
