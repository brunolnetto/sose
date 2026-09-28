from __future__ import annotations

from datetime import datetime, timedelta

from sose.domain.config import DomainConfig

from .simulation import ORIGIN


class TutorialJobConfig(DomainConfig):
    start_at: datetime = ORIGIN
    tick_step: timedelta = timedelta(hours=1)
    random_seed: int = 1701
    complete_after: timedelta = timedelta(hours=2)
    job_key: str = "example"
