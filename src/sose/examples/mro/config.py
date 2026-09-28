from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN, PART_CAPACITY


class MROConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 42

    quantity: float = Field(default=1.0, gt=0, le=PART_CAPACITY)
    technician_capacity: int = Field(default=1, ge=1)
    maintenance_bay_capacity: int = Field(default=1, ge=1)
    spare_part_store_capacity: int = Field(default=10, ge=1)
    release_delay: timedelta = Field(default=timedelta(hours=1), gt=timedelta(0))
