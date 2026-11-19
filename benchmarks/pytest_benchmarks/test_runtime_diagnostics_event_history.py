from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.events import DomainEvent
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.memory import MemoryPersistence


@pytest.fixture(
    params=[0, 1_000, 10_000],
    ids=["0-extra-events", "1000-extra-events", "10000-extra-events"],
)
def prepared_runtime_with_event_history(request):
    extra_events = int(request.param)
    owner_id = "pytest-benchmark-diagnostics-event-history"
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

    # Keep scheduled work and command cardinality fixed while varying only
    # the event history observed by diagnostics.
    baseline_events = len(persistence._state.events)
    persistence._state.events.extend(
        DomainEvent(
            event_id=f"benchmark-event-{index}",
            name="benchmark.observation",
            entity_type="benchmark",
            entity_id=f"entity-{index}",
            occurred_at=ORIGIN + timedelta(seconds=index + 1),
            tick=index + 1,
            owner_id=owner_id,
        )
        for index in range(extra_events)
    )

    return engine, baseline_events + extra_events


def test_engine_diagnostics_event_history_scaling(
    benchmark, prepared_runtime_with_event_history
):
    engine, expected_events = prepared_runtime_with_event_history

    diagnostics = benchmark(engine.diagnostics)

    assert diagnostics.counts.events == expected_events
    assert diagnostics.counts.scheduled_work == 1
