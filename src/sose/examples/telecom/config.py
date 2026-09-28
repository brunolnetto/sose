from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class TelecomConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 947

    customer_id: str = "customer-1"
    product_offering: str = "postpaid-mobile"
    access_technology: str = "5G"
    sim_type: str = "eSIM"
    provisioning_capacity: int = Field(default=1, ge=1)
