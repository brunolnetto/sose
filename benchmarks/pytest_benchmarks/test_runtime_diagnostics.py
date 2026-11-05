"""End-to-end runtime diagnostics benchmarks.

The in-memory case measures diagnostics without durable refresh costs.
The persistent cases measure the complete diagnostics path on local durable
adapters, including their normal refresh behavior.
"""

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
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


PERSISTENT_ADAPTERS = (
    pytest.param(
        SQLitePersistence,
        ".sqlite",
        id="sqlite-snapshot",
    ),
    pytest.param(
        SQLiteIncrementalPersistence,
        ".sqlite",
        id="sqlite-incremental",
    ),
    pytest.param(
        DuckDBPersistence,
        ".duckdb",
        id="duckdb",
    ),
    pytest.param(
        JSONLJournalPersistence,
        ".jsonl",
        id="jsonl-journal",
    ),
)


def _prepare_runtime(persistence, *, owner_id: str, count: int):
    jobs = [
        seed_job(
            persistence,
            owner_id=owner_id,
            job_key=f"job-{index}",
        )
        for index in range(count)
    ]

    _, engine = build_runtime(persistence, owner_id=owner_id)
    engine.rebuild_backend(SimPyBackend(origin=ORIGIN))

    due_at = ORIGIN + timedelta(hours=4)
    for job in jobs:
        start_and_schedule_completion(
            persistence,
            engine,
            job_id=job.id,
            complete_at=due_at,
        )

    return engine


@pytest.mark.parametrize(
    "scheduled_work_count",
    [100, 500],
    ids=["100-scheduled-work", "500-scheduled-work"],
)
def test_engine_diagnostics_memory(benchmark, scheduled_work_count):
    persistence = MemoryPersistence()
    engine = _prepare_runtime(
        persistence,
        owner_id=f"pytest-benchmark-memory-{scheduled_work_count}",
        count=scheduled_work_count,
    )

    diagnostics = benchmark.pedantic(
        engine.diagnostics,
        rounds=7,
        iterations=1,
    )

    assert diagnostics.counts.scheduled_work == scheduled_work_count


@pytest.mark.parametrize("adapter_type,suffix", PERSISTENT_ADAPTERS)
@pytest.mark.parametrize(
    "scheduled_work_count",
    [100, 500],
    ids=["100-scheduled-work", "500-scheduled-work"],
)
def test_engine_diagnostics_persistent(
    benchmark,
    tmp_path,
    adapter_type,
    suffix,
    scheduled_work_count,
):
    adapter_name = adapter_type.__name__
    persistence = adapter_type(
        tmp_path / f"{adapter_name}{suffix}"
    )

    try:
        engine = _prepare_runtime(
            persistence,
            owner_id=(
                f"pytest-benchmark-{adapter_name}-{scheduled_work_count}"
            ),
            count=scheduled_work_count,
        )

        diagnostics = benchmark.pedantic(
            engine.diagnostics,
            rounds=7,
            iterations=1,
        )

        assert diagnostics.counts.scheduled_work == scheduled_work_count
        assert not any(
            issue.code == "scheduled.command_missing"
            for issue in diagnostics.issues
        )
    finally:
        close = getattr(persistence, "close", None)
        if close is not None:
            close()

@pytest.mark.parametrize(
    "scheduled_work_count",
    [100, 500],
    ids=["100-scheduled-work", "500-scheduled-work"],
)
def test_sqlite_incremental_forced_refresh(
    benchmark,
    tmp_path,
    scheduled_work_count,
):
    persistence = SQLiteIncrementalPersistence(
        tmp_path / f"forced-refresh-{scheduled_work_count}.sqlite"
    )

    try:
        _prepare_runtime(
            persistence,
            owner_id=f"pytest-benchmark-forced-refresh-{scheduled_work_count}",
            count=scheduled_work_count,
        )

        benchmark.pedantic(
            lambda: persistence._refresh_from_db(force=True),
            rounds=7,
            iterations=1,
        )

        # Verify the reconstructed state remains usable.
        assert len(persistence.scheduled_work()) == scheduled_work_count
    finally:
        persistence.close()

@pytest.mark.parametrize(
    "scheduled_work_count",
    [100, 500],
    ids=["100-scheduled-work", "500-scheduled-work"],
)
def test_sqlite_incremental_revision_check(
    benchmark,
    tmp_path,
    scheduled_work_count,
):
    persistence = SQLiteIncrementalPersistence(
        tmp_path / f"revision-check-{scheduled_work_count}.sqlite"
    )

    try:
        _prepare_runtime(
            persistence,
            owner_id=f"pytest-benchmark-revision-{scheduled_work_count}",
            count=scheduled_work_count,
        )

        benchmark.pedantic(
            persistence._database_revision,
            rounds=15,
            iterations=100,
        )
    finally:
        persistence.close()
