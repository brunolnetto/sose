from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sose.examples.catalog import builtin_catalog
from sose.jobs.model import CompletedJobTrigger, SimulationJobState
from sose.jobs.runner import (
    JobTickResult,
    SimulationJob,
    scheduled_trigger_id,
)
from sose.core.runtime import SimulationPosition
from sose.persistence.memory import MemoryPersistence
from sose.sinks.base import SinkBinding


NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def _definition(*, write_position: bool = True, reconcile=None):
    base = builtin_catalog().get("tutorial_job")

    def seed(persistence, config):
        return {"seeded": True}

    def build_runtime(persistence, config, logical_time, logical_tick):
        target_time = logical_time + config.tick_step
        context = SimpleNamespace(clock=SimpleNamespace(now=target_time))

        class Engine:
            def rebuild_backend(self, backend):
                self.backend = backend

            def advance_tick(self):
                if write_position:
                    persistence._state.simulation_position = SimulationPosition(
                        logical_time=target_time,
                        execution_sequence=0,
                        committed_sequence=0,
                        logical_tick=logical_tick + 1,
                    )

        return context, Engine()

    return replace(
        base,
        seed=seed,
        build_runtime=build_runtime,
        reconcile_tick=reconcile,
    )


def _job(
    persistence: MemoryPersistence | None = None,
    *,
    definition=None,
    job_id: str = "job",
    ticks_per_trigger: int = 1,
    max_ticks_per_trigger: int = 100,
    sink_bindings=(),
):
    persistence = persistence or MemoryPersistence()
    return SimulationJob(
        job_id=job_id,
        definition=definition or _definition(),
        persistence=persistence,
        backend_factory=lambda origin: object(),
        ticks_per_trigger=ticks_per_trigger,
        max_ticks_per_trigger=max_ticks_per_trigger,
        sink_bindings=sink_bindings,
    )


def _pending_state(job: SimulationJob) -> SimulationJobState:
    config = job.definition.parse_config()
    return SimulationJobState(
        job_id=job.job_id,
        domain_name=job.definition.name,
        config_json=config.model_dump_json(),
        config_revision=1,
        status="ready",
        initialized=False,
        logical_time=config.start_at,
        next_tick=0,
    )


def _replace_transaction_state(
    monkeypatch,
    persistence: MemoryPersistence,
    *,
    target_call: int,
    transform,
):
    original = persistence.transaction
    calls = {"count": 0}

    @contextmanager
    def transaction():
        calls["count"] += 1
        with original() as uow:
            if calls["count"] == target_call:
                current = uow.get_job_state("job")
                transform(uow, current)
            yield uow

    monkeypatch.setattr(persistence, "transaction", transaction)
    return calls


def test_scheduled_trigger_and_constructor_validation_edges():
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduled_trigger_id("job", datetime(2026, 1, 1, 8))

    with pytest.raises(ValueError, match="job_id cannot be empty"):
        scheduled_trigger_id("", NOW)

    definition = _definition()
    persistence = MemoryPersistence()

    with pytest.raises(ValueError, match="job_id cannot be empty"):
        SimulationJob(
            job_id="",
            definition=definition,
            persistence=persistence,
            backend_factory=lambda origin: object(),
        )
    with pytest.raises(ValueError, match="ticks_per_trigger must be >= 1"):
        _job(ticks_per_trigger=0)
    with pytest.raises(ValueError, match="max_ticks_per_trigger must be >= 1"):
        _job(max_ticks_per_trigger=0)
    with pytest.raises(ValueError, match="cannot exceed"):
        _job(ticks_per_trigger=2, max_ticks_per_trigger=1)


def test_close_attempts_all_unique_stores_and_reraises_first_error():
    job = _job()
    calls = []

    class FailingClose:
        def __init__(self, name):
            self.name = name

        def close(self):
            calls.append(self.name)
            raise RuntimeError(self.name)

    job.domain_warehouse = FailingClose("domain-first")
    job.persistence = FailingClose("engine-second")

    with pytest.raises(RuntimeError, match="domain-first"):
        job.close()

    assert calls == ["domain-first", "engine-second"]


def test_flush_sinks_handles_uninitialized_state_and_binding_failure(monkeypatch):
    binding = SinkBinding("analytics", SimpleNamespace(publish=lambda batch: None))
    persistence = MemoryPersistence()
    job = _job(persistence, sink_bindings=(binding,))

    assert job.flush_sinks() == ()

    job.initialize()
    monkeypatch.setattr(
        job.outbox,
        "flush",
        lambda binding, state: (_ for _ in ()).throw(RuntimeError("sink down")),
    )

    assert job.flush_sinks() == (("analytics", "RuntimeError: sink down"),)


def test_initialize_rejects_existing_job_owned_by_other_domain():
    persistence = MemoryPersistence()
    job = _job(persistence)
    state = _pending_state(job)
    with persistence.transaction() as uow:
        uow.save_job_state(replace(state, domain_name="other-domain"))

    with pytest.raises(ValueError, match="already belongs"):
        job.initialize()


def test_finish_initialization_detects_disappearance(monkeypatch):
    persistence = MemoryPersistence()
    job = _job(persistence)
    pending = _pending_state(job)
    with persistence.transaction() as uow:
        uow.save_job_state(pending)

    _replace_transaction_state(
        monkeypatch,
        persistence,
        target_call=1,
        transform=lambda uow, current: uow._working.job_states.pop("job", None),
    )

    with pytest.raises(RuntimeError, match="disappeared during initialization"):
        job._finish_initialization(pending)


def test_finish_initialization_returns_concurrent_initialized_state():
    persistence = MemoryPersistence()
    job = _job(persistence)
    pending = _pending_state(job)
    initialized = replace(
        pending,
        initialized=True,
        bootstrap_state={"winner": True},
    )
    with persistence.transaction() as uow:
        uow.save_job_state(initialized)

    assert job._finish_initialization(pending) == initialized


def test_apply_config_initializes_while_update_and_status_require_existing_job():
    persistence = MemoryPersistence()
    job = _job(persistence)

    applied = job.apply_config({"random_seed": 77})
    assert applied.initialized
    assert applied.config_revision == 1

    missing = _job(MemoryPersistence(), job_id="missing")
    with pytest.raises(RuntimeError, match="job is not initialized"):
        missing.update_config({"random_seed": 2})
    with pytest.raises(RuntimeError, match="job is not initialized"):
        missing.pause()


def test_apply_config_returns_current_when_no_changes():
    persistence = MemoryPersistence()
    job = _job(persistence)
    current = job.initialize()
    same = job.definition.parse_config()

    assert job.apply_config(same) == current


def test_update_config_skips_runtime_validation_for_uninitialized_state(monkeypatch):
    persistence = MemoryPersistence()
    job = _job(persistence)
    pending = _pending_state(job)
    with persistence.transaction() as uow:
        uow.save_job_state(pending)

    monkeypatch.setattr(
        type(job.definition),
        "validate_runtime_config_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime validation should be skipped before initialization")
        ),
    )

    updated = job.update_config({"random_seed": 99})

    assert updated.initialized is False
    assert updated.config_revision == pending.config_revision + 1


def test_domain_warehouse_helpers_attach_flush_and_filter_pending_sink_deliveries(monkeypatch):
    class Warehouse:
        def entity(self, entity_type, entity_id):
            return None

    persistence = MemoryPersistence()
    warehouse = Warehouse()
    job = _job(persistence, definition=_definition(), sink_bindings=(),)
    job.domain_warehouse = warehouse
    job.domain_persistence = persistence
    job.domain_outbox = SimpleNamespace(flush=lambda: 3)

    assert job.flush_domain_warehouse() == 3
    engine = SimpleNamespace()
    job._attach_domain_warehouse(engine)
    assert engine.domain_warehouse is warehouse
    assert engine.domain_entities.entity("missing", "id") is None

    monkeypatch.setattr(
        persistence,
        "sink_deliveries",
        lambda job_id=None: (
            SimpleNamespace(delivery_id="d-pending", status="pending"),
            SimpleNamespace(delivery_id="d-done", status="delivered"),
        ),
    )

    pending = job.pending_sink_deliveries()
    assert [delivery.delivery_id for delivery in pending] == ["d-pending"]


@pytest.mark.parametrize("mode", ["missing", "revision"])
def test_update_config_detects_transaction_races(monkeypatch, mode):
    persistence = MemoryPersistence()
    job = _job(persistence)
    current = job.initialize()

    def transform(uow, latest):
        if mode == "missing":
            uow._working.job_states.pop("job", None)
        else:
            uow._working.job_states["job"] = replace(
                latest,
                config_revision=current.config_revision + 1,
            )

    _replace_transaction_state(
        monkeypatch,
        persistence,
        target_call=1,
        transform=transform,
    )

    message = "disappeared while updating config" if mode == "missing" else "changed concurrently"
    with pytest.raises(RuntimeError, match=message):
        job.update_config({"random_seed": 99})


def test_run_trigger_validates_identity_tick_count_and_initializes_fresh_job():
    fresh = _job(MemoryPersistence(), ticks_per_trigger=1, max_ticks_per_trigger=2)
    result = fresh.run_trigger(trigger_id="fresh-batch", ticks=1)
    assert result.completed_ticks == 1

    job = _job(MemoryPersistence(), max_ticks_per_trigger=2)
    job.initialize()

    with pytest.raises(ValueError, match="trigger_id cannot be empty"):
        job.run_trigger(trigger_id="")
    with pytest.raises(ValueError, match="ticks must be >= 1"):
        job.run_trigger(trigger_id="zero", ticks=0)
    with pytest.raises(ValueError, match="exceeds max_ticks_per_trigger"):
        job.run_trigger(trigger_id="too-many", ticks=3)


def test_run_trigger_reads_completed_record_from_latest_transaction_state(monkeypatch):
    persistence = MemoryPersistence()
    job = _job(persistence)
    initial = job.initialize()
    completed = CompletedJobTrigger(
        trigger_id="batch-done",
        requested_ticks=1,
        start_tick=0,
        end_tick=1,
        config_revision=initial.config_revision,
        logical_time=initial.logical_time,
        run_count=initial.run_count,
    )
    with persistence.transaction() as uow:
        uow.save_job_state(replace(initial, completed_batch_triggers=(completed,)))

    monkeypatch.setattr(job, "state", lambda: initial)
    result = job.run_trigger(trigger_id="batch-done")
    assert result.trigger_id == "batch-done"


def test_run_trigger_rejects_non_idle_state_and_allows_recovering_owned_batch():
    persistence = MemoryPersistence()
    job = _job(persistence)
    current = job.initialize()
    with persistence.transaction() as uow:
        uow.save_job_state(replace(current, status="paused"))
    with pytest.raises(RuntimeError, match="not idle for batch trigger"):
        job.run_trigger(trigger_id="batch")

    persistence = MemoryPersistence()
    job = _job(persistence)
    current = job.initialize()
    with persistence.transaction() as uow:
        uow.save_job_state(
            replace(
                current,
                active_batch_trigger_id="batch",
                active_batch_total_ticks=1,
                active_batch_completed_ticks=1,
                next_tick=1,
            )
        )
    recovered = job.run_trigger(trigger_id="batch", recover=True)
    assert recovered.trigger_id == "batch"


def test_run_trigger_finishes_pending_initialization():
    persistence = MemoryPersistence()
    job = _job(persistence)
    pending = _pending_state(job)
    with persistence.transaction() as uow:
        uow.save_job_state(pending)

    result = job.run_trigger(trigger_id="pending-init", ticks=1)
    assert result.completed_ticks == 1
    assert job.state().initialized


def test_run_trigger_rejects_top_level_batch_tick_count_mismatch():
    persistence = MemoryPersistence()
    job = _job(persistence)
    state = job.initialize()
    with persistence.transaction() as uow:
        uow.save_job_state(
            replace(
                state,
                active_batch_trigger_id="batch",
                active_batch_total_ticks=2,
                active_batch_completed_ticks=0,
            )
        )

    with pytest.raises(RuntimeError, match="different tick count"):
        job.run_trigger(trigger_id="batch", ticks=3, recover=True)


def test_run_trigger_detects_disappearance_before_batch_claim(monkeypatch):
    persistence = MemoryPersistence()
    job = _job(persistence)
    job.initialize()

    _replace_transaction_state(
        monkeypatch,
        persistence,
        target_call=1,
        transform=lambda uow, current: uow._working.job_states.pop("job", None),
    )

    with pytest.raises(RuntimeError, match="disappeared before batch trigger claim"):
        job.run_trigger(trigger_id="batch")


@pytest.mark.parametrize(
    ("latest_trigger", "ticks", "recover", "message"),
    [
        ("other", None, True, "unresolved batch trigger"),
        ("batch", 3, True, "different tick count"),
        ("batch", None, False, "requires explicit recovery"),
    ],
)
def test_run_trigger_validates_concurrent_active_batch(
    monkeypatch,
    latest_trigger,
    ticks,
    recover,
    message,
):
    persistence = MemoryPersistence()
    job = _job(persistence)
    state = job.initialize()
    latest = replace(
        state,
        active_batch_trigger_id=latest_trigger,
        active_batch_total_ticks=2,
        active_batch_completed_ticks=0,
    )
    with persistence.transaction() as uow:
        uow.save_job_state(latest)

    stale = replace(
        latest,
        active_batch_trigger_id=None,
        active_batch_total_ticks=0,
        active_batch_completed_ticks=0,
    )
    monkeypatch.setattr(job, "state", lambda: stale)

    with pytest.raises(RuntimeError, match=message):
        job.run_trigger(
            trigger_id="batch",
            ticks=ticks,
            recover=recover,
        )


@pytest.mark.parametrize(
    ("target_call", "mode", "message"),
    [
        (2, "missing", "disappeared during batch trigger"),
        (2, "lost", "batch trigger ownership was lost"),
        (3, "missing", "disappeared while completing batch trigger"),
        (3, "lost", "batch trigger ownership was lost"),
    ],
)
def test_run_trigger_detects_progress_and_completion_races(
    monkeypatch,
    target_call,
    mode,
    message,
):
    persistence = MemoryPersistence()
    job = _job(persistence)
    state = job.initialize()

    monkeypatch.setattr(
        job,
        "run_tick",
        lambda **kwargs: JobTickResult(
            job_id="job",
            domain_name=job.definition.name,
            config_revision=1,
            logical_time=state.logical_time,
            logical_tick=1,
            run_count=1,
            trigger_id=kwargs["trigger_id"],
        ),
    )

    def transform(uow, current):
        if mode == "missing":
            uow._working.job_states.pop("job", None)
        else:
            uow._working.job_states["job"] = replace(
                current,
                active_batch_trigger_id="stolen",
            )

    _replace_transaction_state(
        monkeypatch,
        persistence,
        target_call=target_call,
        transform=transform,
    )

    with pytest.raises(RuntimeError, match=message):
        job.run_trigger(trigger_id="batch", ticks=1)


def test_run_tick_finishes_pending_initialization():
    persistence = MemoryPersistence()
    job = _job(persistence)
    pending = _pending_state(job)
    with persistence.transaction() as uow:
        uow.save_job_state(pending)

    result = job.run_tick(trigger_id="tick")
    assert result.logical_tick == 1
    assert job.state().initialized


def test_run_tick_initializes_when_state_is_missing_and_rejects_wrong_batch_child():
    fresh = _job(MemoryPersistence())
    assert fresh.run_tick(trigger_id="tick-1").logical_tick == 1

    persistence = MemoryPersistence()
    job = _job(persistence)
    current = job.initialize()
    with persistence.transaction() as uow:
        uow.save_job_state(
            replace(
                current,
                active_batch_trigger_id="batch",
                active_batch_total_ticks=2,
                active_batch_completed_ticks=0,
            )
        )

    with pytest.raises(RuntimeError, match="expected_child"):
        job.run_tick(trigger_id="batch:tick:9")


def test_pause_rejects_status_change_while_batch_trigger_active():
    persistence = MemoryPersistence()
    job = _job(persistence)
    current = job.initialize()
    with persistence.transaction() as uow:
        uow.save_job_state(
            replace(
                current,
                active_batch_trigger_id="batch",
                active_batch_total_ticks=1,
                active_batch_completed_ticks=0,
            )
        )

    with pytest.raises(RuntimeError, match="cannot change status while job trigger is active"):
        job.pause()


def test_run_tick_detects_disappearance_before_claim(monkeypatch):
    persistence = MemoryPersistence()
    job = _job(persistence)
    job.initialize()

    _replace_transaction_state(
        monkeypatch,
        persistence,
        target_call=1,
        transform=lambda uow, current: uow._working.job_states.pop("job", None),
    )

    with pytest.raises(RuntimeError, match="disappeared before trigger claim"):
        job.run_tick(trigger_id="tick")


def test_run_tick_detects_batch_ownership_change_before_claim(monkeypatch):
    persistence = MemoryPersistence()
    job = _job(persistence)
    current = job.initialize()
    stale = replace(
        current,
        active_batch_trigger_id="batch",
        active_batch_total_ticks=1,
        active_batch_completed_ticks=0,
    )
    monkeypatch.setattr(job, "state", lambda: stale)

    with pytest.raises(RuntimeError, match="batch trigger ownership changed"):
        job.run_tick(trigger_id="batch:tick:1")


def _inject_lost_tick_ownership(
    monkeypatch,
    persistence,
    *,
    target_call,
):
    def transform(uow, current):
        uow._working.job_states["job"] = replace(
            current,
            active_trigger_id="stolen",
        )

    return _replace_transaction_state(
        monkeypatch,
        persistence,
        target_call=target_call,
        transform=transform,
    )


@pytest.mark.parametrize("target_call", [2, 3, 4])
def test_run_tick_detects_ownership_loss_at_durable_checkpoints(
    monkeypatch,
    target_call,
):
    persistence = MemoryPersistence()
    job = _job(persistence)
    job.initialize()

    _inject_lost_tick_ownership(
        monkeypatch,
        persistence,
        target_call=target_call,
    )

    with pytest.raises(RuntimeError, match="trigger ownership was lost"):
        job.run_tick(trigger_id="tick")


def test_run_tick_detects_ownership_loss_during_crash_window_recovery(monkeypatch):
    persistence = MemoryPersistence()
    job = _job(persistence)
    current = job.initialize()
    with persistence.transaction() as uow:
        uow.save_job_state(
            replace(
                current,
                status="failed",
                phase="advance",
                active_trigger_id="tick",
            )
        )
    persistence._state.simulation_position = SimulationPosition(
        logical_time=current.logical_time + timedelta(hours=1),
        execution_sequence=0,
        committed_sequence=0,
        logical_tick=1,
    )

    _inject_lost_tick_ownership(
        monkeypatch,
        persistence,
        target_call=2,
    )

    with pytest.raises(RuntimeError, match="trigger ownership was lost"):
        job.run_tick(trigger_id="tick", recover=True)


def test_run_tick_requires_advance_to_publish_simulation_position():
    persistence = MemoryPersistence()
    job = _job(persistence, definition=_definition(write_position=False))
    job.initialize()

    with pytest.raises(RuntimeError, match="advance completed without durable simulation position"):
        job.run_tick(trigger_id="tick")


def test_run_tick_reconcile_phase_requires_existing_position():
    persistence = MemoryPersistence()
    job = _job(persistence)
    current = job.initialize()
    with persistence.transaction() as uow:
        uow.save_job_state(
            replace(
                current,
                status="failed",
                phase="reconcile",
                active_trigger_id="tick",
            )
        )

    with pytest.raises(RuntimeError, match="reconcile phase requires durable simulation position"):
        job.run_tick(trigger_id="tick", recover=True)


def test_run_tick_requires_position_after_reconciliation():
    persistence = MemoryPersistence()

    def clear_position(persistence, engine, backend, config, bootstrap):
        persistence._state.simulation_position = None

    job = _job(
        persistence,
        definition=_definition(reconcile=clear_position),
    )
    job.initialize()

    with pytest.raises(RuntimeError, match="tick completed without a durable simulation position"):
        job.run_tick(trigger_id="tick")
