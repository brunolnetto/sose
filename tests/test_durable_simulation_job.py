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


def test_explicit_trigger_id_is_idempotent_after_success():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="idempotent-trigger",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    job.initialize({"complete_after": timedelta(hours=3)})

    first = job.run_tick(trigger_id="scheduler-run-001")
    repeated = job.run_tick(trigger_id="scheduler-run-001")

    assert repeated == first
    assert repeated.trigger_id == "scheduler-run-001"
    assert persistence.simulation_position().logical_tick == 1
    assert job.state().run_count == 1
    assert job.state().last_completed_trigger_id == "scheduler-run-001"
    assert job.state().active_trigger_id is None


def test_overlapping_trigger_is_rejected_while_job_is_claimed():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    overlap_errors: list[str] = []

    second_job = SimulationJob(
        job_id="overlap-job",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )

    def inspecting_backend(origin):
        try:
            second_job.run_tick(trigger_id="overlap-B")
        except RuntimeError as exc:
            overlap_errors.append(str(exc))
        return SimPyBackend(origin=origin)

    first_job = SimulationJob(
        job_id="overlap-job",
        definition=definition,
        persistence=persistence,
        backend_factory=inspecting_backend,
    )
    first_job.initialize()

    result = first_job.run_tick(trigger_id="overlap-A")

    assert result.logical_tick == 1
    assert result.trigger_id == "overlap-A"
    assert overlap_errors
    assert "already running" in overlap_errors[0]
    assert "overlap-A" in overlap_errors[0]


def test_trigger_identity_survives_sqlite_reopen(tmp_path):
    path = tmp_path / "trigger-identity.sqlite3"
    definition = builtin_catalog().get("tutorial_job")

    first_persistence = SQLiteIncrementalPersistence(path)
    first_job = SimulationJob(
        job_id="trigger-reopen",
        definition=definition,
        persistence=first_persistence,
        backend_factory=_backend,
    )
    first_job.initialize()
    first = first_job.run_tick(trigger_id="external-42")
    first_persistence.close()

    reopened = SQLiteIncrementalPersistence(path)
    resumed = SimulationJob(
        job_id="trigger-reopen",
        definition=definition,
        persistence=reopened,
        backend_factory=_backend,
    )
    repeated = resumed.run_tick(trigger_id="external-42")

    assert repeated == first
    assert reopened.simulation_position().logical_tick == 1
    assert resumed.state().last_completed_trigger_id == "external-42"
    reopened.close()


def test_failed_before_advance_can_recover_same_trigger():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    attempts = {"count": 0}

    def flaky_backend(origin):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("backend unavailable")
        return SimPyBackend(origin=origin)

    job = SimulationJob(
        job_id="recover-before-advance",
        definition=definition,
        persistence=persistence,
        backend_factory=flaky_backend,
    )
    job.initialize()

    with pytest.raises(RuntimeError, match="backend unavailable"):
        job.run_tick(trigger_id="retry-advance")

    failed = job.state()
    assert failed.status == "failed"
    assert failed.phase == "advance"
    assert failed.active_trigger_id == "retry-advance"
    assert persistence.simulation_position() is None

    result = job.run_tick(
        trigger_id="retry-advance",
        recover=True,
    )

    assert result.logical_tick == 1
    assert result.trigger_id == "retry-advance"
    assert job.state().status == "ready"
    assert job.state().phase == "idle"


def test_different_trigger_cannot_bypass_failed_owned_phase():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")

    def fail_backend(origin):
        raise RuntimeError("boom")

    job = SimulationJob(
        job_id="failed-owned-trigger",
        definition=definition,
        persistence=persistence,
        backend_factory=fail_backend,
    )
    job.initialize()

    with pytest.raises(RuntimeError, match="boom"):
        job.run_tick(trigger_id="original-trigger")

    with pytest.raises(RuntimeError, match="unresolved trigger"):
        job.run_tick(trigger_id="different-trigger")

    with pytest.raises(RuntimeError, match="unresolved"):
        job.update_config({"random_seed": 99})

    with pytest.raises(RuntimeError, match="must be recovered"):
        job.resume()


def test_reconcile_failure_recovers_without_advancing_second_tick():
    persistence = MemoryPersistence()
    base = builtin_catalog().get("tutorial_job")
    calls = {"count": 0}

    def flaky_reconcile(persistence, engine, backend, config, bootstrap):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("reconcile interrupted")
        assert base.reconcile_tick is not None
        base.reconcile_tick(
            persistence,
            engine,
            backend,
            config,
            bootstrap,
        )

    definition = replace(base, reconcile_tick=flaky_reconcile)
    job = SimulationJob(
        job_id="recover-reconcile",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    state = job.initialize({"complete_after": timedelta(hours=3)})

    with pytest.raises(RuntimeError, match="reconcile interrupted"):
        job.run_tick(trigger_id="reconcile-trigger")

    failed = job.state()
    assert failed.status == "failed"
    assert failed.phase == "reconcile"
    assert failed.next_tick == 1
    assert persistence.simulation_position().logical_tick == 1

    result = job.run_tick(
        trigger_id="reconcile-trigger",
        recover=True,
    )

    assert result.logical_tick == 1
    assert result.run_count == 1
    assert persistence.simulation_position().logical_tick == 1
    entity = persistence.entity("tutorial_job", state.bootstrap_state.id)
    assert entity is not None and entity.state == "running"


def test_recovery_infers_reconcile_when_position_committed_before_phase_checkpoint():
    persistence = MemoryPersistence()
    definition = builtin_catalog().get("tutorial_job")
    job = SimulationJob(
        job_id="crash-window",
        definition=definition,
        persistence=persistence,
        backend_factory=_backend,
    )
    state = job.initialize({"complete_after": timedelta(hours=3)})

    claimed = replace(
        state,
        status="running",
        phase="advance",
        active_trigger_id="crash-trigger",
    )
    with persistence.transaction() as uow:
        uow.save_job_state(claimed)

    config = definition.config_model.model_validate_json(claimed.config_json)
    context, engine = definition.build_runtime(
        persistence,
        config,
        config.start_at,
        0,
    )
    backend = _backend(config.start_at)
    engine.rebuild_backend(backend)
    engine.advance_tick()
    backend.run_until(context.clock.now)

    # Simulates process death here: SimulationPosition committed, job phase did not.
    assert persistence.simulation_position().logical_tick == 1
    assert job.state().phase == "advance"
    assert job.state().next_tick == 0

    result = job.run_tick(
        trigger_id="crash-trigger",
        recover=True,
    )

    assert result.logical_tick == 1
    assert result.run_count == 1
    assert job.state().phase == "idle"
    assert job.state().last_completed_trigger_id == "crash-trigger"
