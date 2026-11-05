from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.memory import MemoryPersistence


@pytest.fixture(
    params=[100, 500],
    ids=["100-scheduled-work", "500-scheduled-work"],
)
def prepared_runtime(request):
    count = int(request.param)
    persistence = MemoryPersistence()
    owner_id = f"pytest-benchmark-diagnostics-{count}"
    jobs = [
        seed_job(persistence, owner_id=owner_id, job_key=f"job-{index}")
        for index in range(count)
    ]

    _, engine = build_runtime(persistence, owner_id=owner_id)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    due_at = ORIGIN + timedelta(hours=4)

    for job in jobs:
        start_and_schedule_completion(
            persistence,
            engine,
            job_id=job.id,
            complete_at=due_at,
        )

    return engine, count


def test_engine_diagnostics(benchmark, prepared_runtime):
    engine, expected_scheduled_work = prepared_runtime

    diagnostics = benchmark(engine.diagnostics)

    assert diagnostics.counts.scheduled_work == expected_scheduled_work
