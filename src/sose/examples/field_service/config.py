from datetime import datetime, timedelta

from pydantic import Field

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class FieldServiceConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 1223

    territory: str = "west"
    required_skill: str = "fiber-installation"
    wrong_skill: str = "copper-installation"
    technician_resource_capacity: int = Field(default=1, ge=1)
    parts_store_capacity: int = Field(default=20, ge=1)
