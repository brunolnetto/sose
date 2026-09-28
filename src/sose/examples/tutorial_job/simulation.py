from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.api import (
    DomainRegistry,
    Engine,
    EntityType,
    MemoryPersistence,
    RandomSource,
    SimulationClock,
    SimulationContext,
)
from sose.backends.simpy import SimPyBackend
from sose.core.scheduler import Scheduler

from .entities import TutorialJob
from .statecharts import TutorialJobChart


ORIGIN = datetime(2027, 4, 1, 9, tzinfo=timezone.utc)


def build_runtime(
    persistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
):
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=1701),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("tutorial_job", TutorialJobChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
    )


def seed_job(persistence, *, job_key: str = "example") -> TutorialJob:
    context, _ = build_runtime(persistence)
    job = context.entities.create(
        TutorialJob,
        key=("tutorial-job", job_key),
        state="queued",
        attributes={"job_key": job_key},
    )
    with persistence.transaction() as uow:
        uow.save_entity(job)
    return job


def start_and_schedule_completion(
    persistence,
    engine: Engine,
    *,
    job_id: str,
    complete_at: datetime,
) -> None:
    job = persistence.entity("tutorial_job", job_id)
    if job is None:
        raise RuntimeError(f"tutorial job not found: {job_id}")
    if job.state == "queued":
        start = engine.context.commands.create(
            "start",
            target=job,
            correlation_id=job.id,
            key=("tutorial-job", job.id, "start"),
        )
        engine.dispatch(start)
        job = persistence.entity("tutorial_job", job.id)

    if job is None or job.state != "running":
        raise RuntimeError("completion can only be scheduled for a running job")

    if engine.scheduler.find_pending(
        entity_type="tutorial_job",
        entity_id=job.id,
        name="finish",
    ) is None:
        finish = engine.context.commands.create(
            "finish",
            target=job,
            due_at=complete_at,
            correlation_id=job.id,
            key=("tutorial-job", job.id, "finish"),
        )
        engine.context.schedules.at(complete_at, command=finish)


def run_happy_path():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    complete_at = ORIGIN + timedelta(hours=2)
    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=complete_at,
    )
    backend.run_until(complete_at)
    return persistence, job.id
