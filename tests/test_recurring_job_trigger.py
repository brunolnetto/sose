from dataclasses import replace
from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def _backend(origin):
    return SimPyBackend(origin=origin)


def test_recurring_trigger_advances_configured_number_of_ticks():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="batch-job",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
        ticks_per_trigger=2,
        max_ticks_per_trigger=5,
    )
    state = job.initialize({"complete_after": timedelta(hours=3)})

    result = job.run_trigger(trigger_id="scheduler-001")

    assert result.requested_ticks == 2
    assert result.completed_ticks == 2
    assert result.start_tick == 0
    assert result.end_tick == 2
    assert job.state().next_tick == 2
    entity = persistence.entity("tutorial_job", state.bootstrap_state.id)
    assert entity is not None and entity.state == "running"


def test_replaying_completed_batch_trigger_is_idempotent():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="batch-idempotent",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
        ticks_per_trigger=2,
    )
    job.initialize({"complete_after": timedelta(hours=4)})

    first = job.run_trigger(trigger_id="scheduler-001")
    repeated = job.run_trigger(trigger_id="scheduler-001")

    assert repeated == first
    assert job.state().next_tick == 2
    assert persistence.simulation_position().logical_tick == 2


def test_failed_batch_recovers_from_first_unfinished_child_tick():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    attempts = {"count": 0}

    def flaky_backend(origin):
        attempts["count"] += 1
        if attempts["count"] == 2:
            raise RuntimeError("second tick backend failure")
        return SimPyBackend(origin=origin)

    job = SimulationJob(
        job_id="batch-recovery",
        definition=definition,
        persistence=persistence,
        backend_factory=flaky_backend,
        ticks_per_trigger=3,
        max_ticks_per_trigger=5,
    )
    job.initialize({"complete_after": timedelta(hours=5)})

    with pytest.raises(RuntimeError, match="second tick backend failure"):
        job.run_trigger(trigger_id="scheduler-002")

    failed = job.state()
    assert failed.active_batch_trigger_id == "scheduler-002"
    assert failed.active_batch_total_ticks == 3
    assert failed.active_batch_completed_ticks == 1
    assert failed.active_trigger_id == "scheduler-002:tick:2"
    assert failed.next_tick == 1

    recovered = job.run_trigger(
        trigger_id="scheduler-002",
        recover=True,
    )

    assert recovered.completed_ticks == 3
    assert recovered.start_tick == 0
    assert recovered.end_tick == 3
    final = job.state()
    assert final.active_batch_trigger_id is None
    assert final.last_completed_batch_trigger_id == "scheduler-002"
    assert final.last_completed_batch_ticks == 3
    assert final.next_tick == 3


def test_active_batch_blocks_direct_tick_and_config_change():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="batch-ownership",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    state = job.initialize()

    with persistence.transaction() as uow:
        current = uow.get_job_state(job.job_id)
        assert current is not None
        uow.save_job_state(
            replace(
                current,
                active_batch_trigger_id="scheduler-003",
                active_batch_total_ticks=2,
                active_batch_completed_ticks=1,
            )
        )

    with pytest.raises(RuntimeError, match="unresolved batch trigger"):
        job.run_tick(trigger_id="manual-tick")

    with pytest.raises(RuntimeError, match="cannot change config"):
        job.update_config({"random_seed": 123})

    with pytest.raises(RuntimeError, match="cannot change status"):
        job.pause()


def test_batch_tick_count_is_bounded():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="bounded-batch",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
        ticks_per_trigger=1,
        max_ticks_per_trigger=3,
    )
    job.initialize()

    with pytest.raises(ValueError, match="max_ticks_per_trigger"):
        job.run_trigger(trigger_id="too-many", ticks=4)


def test_batch_trigger_progress_survives_sqlite_reopen(tmp_path):
    path = tmp_path / "batch.sqlite3"
    definition = builtin_catalog().get("tutorial_job")
    attempts = {"count": 0}

    def fail_second(origin):
        attempts["count"] += 1
        if attempts["count"] == 2:
            raise RuntimeError("stop")
        return SimPyBackend(origin=origin)

    first_persistence = SQLiteIncrementalPersistence(path)
    first = SimulationJob(
        job_id="batch-reopen",
        definition=definition,
        persistence=first_persistence,
        backend_factory=fail_second,
        ticks_per_trigger=3,
    )
    first.initialize({"complete_after": timedelta(hours=5)})

    with pytest.raises(RuntimeError, match="stop"):
        first.run_trigger(trigger_id="external-batch")
    first_persistence.close()

    reopened = SQLiteIncrementalPersistence(path)
    resumed = SimulationJob(
        job_id="batch-reopen",
        definition=definition,
        persistence=reopened,
        backend_factory=_backend,
        ticks_per_trigger=3,
    )

    result = resumed.run_trigger(
        trigger_id="external-batch",
        recover=True,
    )

    assert result.end_tick == 3
    assert resumed.state().last_completed_batch_trigger_id == "external-batch"
    reopened.close()
