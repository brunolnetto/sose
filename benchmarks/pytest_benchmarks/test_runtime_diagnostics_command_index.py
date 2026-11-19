from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.events import Command
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.memory import MemoryPersistence


@pytest.fixture(
    params=[0, 1_000, 10_000],
    ids=["1-command", "1001-commands", "10001-commands"],
)
def prepared_runtime_with_command_index(request):
    extra_commands = int(request.param)
    owner_id = "pytest-benchmark-diagnostics-command-index"
    persistence = MemoryPersistence()
    job = seed_job(persistence, owner_id=owner_id, job_key="baseline-job")

    _, engine = build_runtime(persistence, owner_id=owner_id)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=ORIGIN + timedelta(hours=4),
    )

    # Keep scheduled work fixed at one while varying the size of the command
    # index. The extra commands are intentionally not scheduled.
    for index in range(extra_commands):
        command_id = f"benchmark-unreferenced-command-{index}"
        persistence._state.commands[command_id] = Command(
            command_id=command_id,
            name="benchmark.unreferenced",
            entity_type="benchmark",
            entity_id=f"entity-{index}",
            due_at=ORIGIN,
            owner_id=owner_id,
        )

    return engine, len(persistence._state.commands)


def test_engine_diagnostics_command_index_scaling(
    benchmark, prepared_runtime_with_command_index
):
    engine, expected_command_count = prepared_runtime_with_command_index

    diagnostics = benchmark(engine.diagnostics)

    assert diagnostics.counts.scheduled_work == 1
    assert len(engine.persistence._state.commands) == expected_command_count
