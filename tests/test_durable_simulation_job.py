from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs import SimulationJob
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def _backend(origin):
    return SimPyBackend(origin=origin)


@pytest.mark.parametrize("factory", [MemoryPersistence])
def test_recurring_job_advances_exactly_one_tick_per_trigger(factory):
    persistence = factory()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="tutorial-recurring",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )

    state = job.initialize(
        {
            "tick_step": timedelta(hours=1),
            "complete_after": timedelta(hours=2),
        }
    )
    assert state.initialized
    assert state.next_tick == 0
    assert state.run_count == 0

    first = job.run_tick()
    assert first.logical_tick == 1
    assert first.run_count == 1
    assert persistence.entity(
        "tutorial_job",
        state.bootstrap_state.id,
    ).state == "running"

    second = job.run_tick()
    assert second.logical_tick == 2
    assert second.run_count == 2
    assert persistence.entity(
        "tutorial_job",
        state.bootstrap_state.id,
    ).state == "completed"


def test_job_resumes_from_persisted_position_with_fresh_process_object(tmp_path):
    path = tmp_path / "job.sqlite3"
    definition = builtin_catalog().get("tutorial_job")

    first_persistence = SQLiteIncrementalPersistence(path)
    first_job = SimulationJob(
        job_id="persistent-job",
        definition=definition,
        persistence=first_persistence,
        backend_factory=_backend,
    )
    first_job.initialize({"complete_after": timedelta(hours=3)})
    first = first_job.run_tick()
    first_persistence.close()

    reopened = SQLiteIncrementalPersistence(path)
    resumed_job = SimulationJob(
        job_id="persistent-job",
        definition=definition,
        persistence=reopened,
        backend_factory=_backend,
    )
    second = resumed_job.run_tick()

    assert first.logical_tick == 1
    assert second.logical_tick == 2
    assert second.run_count == 2
    assert resumed_job.state().logical_time == second.logical_time
    reopened.close()


def test_config_can_change_between_ticks_and_is_revisioned():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="configurable-job",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    job.initialize()
    first = job.run_tick()

    updated = job.update_config(
        {
            "tick_step": timedelta(minutes=30),
            "complete_after": timedelta(hours=2),
            "random_seed": 9001,
        }
    )
    second = job.run_tick()

    assert first.config_revision == 1
    assert updated.config_revision == 2
    assert second.config_revision == 2
    assert second.logical_time - first.logical_time == timedelta(minutes=30)


def test_paused_job_does_not_advance():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="paused-job",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    job.initialize()
    job.pause()

    with pytest.raises(RuntimeError, match="paused"):
        job.run_tick()

    assert persistence.simulation_position() is None
    assert job.state().run_count == 0


def test_mro_recurring_job_uses_domain_reconcile_hook():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("mro")
    job = SimulationJob(
        job_id="mro-job",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    state = job.initialize(
        {
            "quantity": 2.0,
            "technician_capacity": 2,
            "maintenance_bay_capacity": 2,
            "release_delay": timedelta(hours=1),
        }
    )

    result = job.run_tick()
    work_order = persistence.entity(
        "work_order",
        state.bootstrap_state.work_order_id,
    )

    assert result.logical_tick == 1
    assert work_order is not None
    assert work_order.state == "in_progress"
    assert persistence.store_get_results()
    assert persistence.container_operation_results()


def test_failed_tick_persists_operational_failure():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")

    def fail_backend(origin):
        raise RuntimeError("boom")

    job = SimulationJob(
        job_id="failed-job",
        definition=definition,
        persistence=persistence,
        backend_factory=fail_backend,
    )
    job.initialize()

    with pytest.raises(RuntimeError, match="boom"):
        job.run_tick()

    state = job.state()
    assert state.status == "failed"
    assert "RuntimeError: boom" in state.last_error
