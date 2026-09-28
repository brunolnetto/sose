from __future__ import annotations

from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import TutorialJobConfig
from .simulation import (
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)


def _build(
    persistence: Persistence,
    config: TutorialJobConfig,
    now: datetime,
    tick: int,
):
    return build_runtime(
        persistence,
        now=now,
        tick=tick,
        step=config.tick_step,
        random_seed=config.random_seed,
    )


def _seed(persistence: Persistence, config: TutorialJobConfig):
    return seed_job(persistence, job_key=config.job_key)


def _reconcile_tick(
    persistence: Persistence,
    engine,
    backend,
    config: TutorialJobConfig,
    job,
) -> None:
    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=config.start_at + config.complete_after,
    )


definition = DomainDefinition(
    name="tutorial_job",
    description="Minimal queued -> running -> completed durable tutorial domain.",
    config_model=TutorialJobConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
)
