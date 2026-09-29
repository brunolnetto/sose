from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class SubscriptionSaaSConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 1531

    customer_id: str = "customer-1"
    initial_plan: str = "basic"
    term_duration: timedelta = Field(
        default=timedelta(days=30),
        gt=timedelta(0),
    )
    target_plan: str | None = "pro"
    plan_change_after: timedelta = Field(
        default=timedelta(days=5),
        gt=timedelta(0),
    )
    auto_progress_plan_change: bool = True
