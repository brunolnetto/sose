from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class WarehouseManagementConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 5505

    origin_on_hand: float = Field(default=20.0, ge=0.0)
    transfer_quantity: float = Field(default=8.0, gt=0.0)
    dock_count: int = Field(default=2, ge=1)
    forklift_count: int = Field(default=2, ge=1)
    planned_completion_hours: float = Field(default=4.0, gt=0.0)
    auto_progress_transfer: bool = True
