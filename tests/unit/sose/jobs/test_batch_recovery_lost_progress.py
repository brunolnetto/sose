"""A batch recovered after its progress save was lost keeps its exact history (#496).

Batch progress is saved after each child tick in its own transaction. Found by the
composed PostgreSQL equivalence test: a worker that dies after a child tick commits
but before that save made the recovered batch record start at the advanced
next_tick (e.g. 3->3 for a one-tick batch that ran 2->3).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence

T0 = datetime(2026, 11, 22, 8, tzinfo=timezone.utc)


def _job(persistence):
    return SimulationJob(
        job_id="batch-recovery", definition=builtin_catalog().get("tutorial_job"),
        persistence=persistence, backend_factory=lambda origin: SimPyBackend(origin=origin),
        ticks_per_trigger=3, max_ticks_per_trigger=3,
    )


def _reference():
    persistence = MemoryPersistence()
    job = _job(persistence)
    job.initialize({"random_seed": 7})
    job.run_trigger(trigger_id="warmup", triggered_at=T0)
    job.run_trigger(trigger_id="batch", triggered_at=T0)
    return persistence.job_state("batch-recovery"), persistence.events()


@pytest.mark.parametrize("lost_after_tick", [1, 2, 3])
def test_recovery_after_a_lost_progress_save_matches_the_uninterrupted_run(
    monkeypatch, lost_after_tick,
):
    persistence = MemoryPersistence()
    job = _job(persistence)
    job.initialize({"random_seed": 7})
    job.run_trigger(trigger_id="warmup", triggered_at=T0)

    real_save = SimulationJob._save_batch_progress

    def die_before_saving(self, *, trigger_id, completed):
        if completed == lost_after_tick:
            raise RuntimeError("worker died before saving batch progress")
        return real_save(self, trigger_id=trigger_id, completed=completed)

    monkeypatch.setattr(SimulationJob, "_save_batch_progress", die_before_saving)
    with pytest.raises(RuntimeError, match="before saving batch progress"):
        job.run_trigger(trigger_id="batch", triggered_at=T0)
    monkeypatch.undo()
    interrupted = persistence.job_state("batch-recovery")
    assert interrupted.last_completed_trigger_id == f"batch:tick:{lost_after_tick}"
    assert interrupted.active_batch_completed_ticks == lost_after_tick - 1

    result = _job(persistence).run_trigger(trigger_id="batch", triggered_at=T0, recover=True)
    reference_state, reference_events = _reference()
    state = persistence.job_state("batch-recovery")
    assert state.completed_batch_triggers == reference_state.completed_batch_triggers
    assert (result.start_tick, result.end_tick) == (3, 6)
    assert state.next_tick == reference_state.next_tick == 6
    assert persistence.events() == reference_events        # no tick ran twice


@pytest.mark.parametrize("died_in_tick", [1, 2, 3])
def test_recovery_after_dying_mid_tick_with_next_tick_advanced(monkeypatch, died_in_tick):
    """The case the PostgreSQL test found: the child tick's advance committed
    next_tick, then the worker died before the tick's reconcile phase finished."""
    persistence = MemoryPersistence()
    job = _job(persistence)
    job.initialize({"random_seed": 7})
    job.run_trigger(trigger_id="warmup", triggered_at=T0)

    real_reconcile = SimulationJob._run_reconcile_tick
    calls = {"n": 0}

    def die_in_reconcile(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == died_in_tick:
            raise RuntimeError("worker died during the tick's reconcile phase")
        return real_reconcile(self, *args, **kwargs)

    monkeypatch.setattr(SimulationJob, "_run_reconcile_tick", die_in_reconcile)
    with pytest.raises(RuntimeError, match="reconcile phase"):
        job.run_trigger(trigger_id="batch", triggered_at=T0)
    monkeypatch.undo()
    interrupted = persistence.job_state("batch-recovery")
    assert interrupted.phase == "reconcile"
    assert interrupted.next_tick == 3 + died_in_tick           # already advanced

    result = _job(persistence).run_trigger(trigger_id="batch", triggered_at=T0, recover=True)
    reference_state, reference_events = _reference()
    state = persistence.job_state("batch-recovery")
    assert (result.start_tick, result.end_tick) == (3, 6)
    assert state.completed_batch_triggers == reference_state.completed_batch_triggers
    assert persistence.events() == reference_events
    assert state.active_batch_start_tick is None               # cleared on completion


@pytest.mark.parametrize("fields, message", [
    ({"active_batch_start_tick": 2}, "requires an active batch"),
    ({"active_batch_start_tick": 9, "active_batch_trigger_id": "b", "active_batch_total_ticks": 1},
     "cannot exceed next_tick"),
    ({"active_batch_start_tick": -1, "active_batch_trigger_id": "b", "active_batch_total_ticks": 1},
     ">= 0"),
])
def test_the_recorded_start_is_consistent_with_the_checkpoint(fields, message):
    from sose.jobs.model import SimulationJobState

    with pytest.raises(ValueError, match=message):
        SimulationJobState(
            job_id="j", domain_name="d", config_json="{}", config_revision=1,
            status="ready", initialized=True, logical_time=T0, next_tick=3, **fields,
        )
