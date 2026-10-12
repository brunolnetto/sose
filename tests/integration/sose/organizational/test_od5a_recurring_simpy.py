"""First OD-5a recurring SimPy dispatch/fencing slice, not full #485 promotion.

A real ephemeral SimPy callback must commit the authoritative source action
AND per-organization SimulationJobState checkpoint in one fenced UoW. No
SimPy object may be serialized. Boundary reconciliation remains independent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from multiprocessing import get_context

import pytest

from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_durable import (
    initialize_od5a_durable, advance_od5a_durable, od5a_durable_events,
    od5a_durable_state,
)
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_recurring import (
    initialize_od5a_recurring_job, claim_od5a_recurring_tick,
    execute_od5a_claimed_tick, run_od5a_recurring_tick,
    StaleOD5aRunnerClaimError,
)
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.memory import MemoryUnitOfWork
from sose.core.identity import deterministic_id


CLOCK = datetime(2026, 10, 12, tzinfo=timezone.utc)
LEASE = timedelta(seconds=5)


def _arms():
    model = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"], ["a", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8, "execution_slots": 1,
            "coordination_latency": 0.2, "service_cv": 0.5,
        },
        parameter_evidence={
            x: EvidenceClass.ASSUMED for x in
            ("arrival_rate", "execution_slots", "coordination_latency", "service_cv")
        },
    )
    return {x.arm_id: x for x in compile_od5a_arms(
        baseline=model, expanded_capacity=2,
        removed_dependency_edges=(("a", "c"),),
        reduced_coordination_latency=0.1,
        declared_costs={
            x: OD5aChargedCost(8, 1, 1, 1) for x in (
                "capacity_only", "topology_only", "coordination_only",
                "structural_combined",
            )
        },
    )}


def _items():
    return (
        OD5aWorkItem("item:A", 0.0, (("a", 0.1), ("b", 0.2), ("c", 0.3))),
        OD5aWorkItem("item:B", 0.15, (("a", 0.25), ("b", 0.2), ("c", 0.1))),
    )


def _init(path, *, job_id="org:A", arm="baseline"):
    with SQLitePersistence(path) as store:
        initialize_od5a_recurring_job(
            store, job_id=job_id, arm=_arms()[arm], items=_items(),
        )


def _trigger(store, job_id="org:A"):
    state = store.job_state(job_id)
    assert state is not None
    return deterministic_id("od5a-recurring-tick", job_id, state.run_count + 1)


def _snapshot(path, job_id):
    with SQLitePersistence(path) as store:
        return (
            od5a_durable_state(store, job_id=job_id),
            od5a_durable_events(store, job_id=job_id),
            store.resource_reservations(),
        )


def _drain(path, *, job_id="org:A", owner="worker:A"):
    actions = []
    with SQLitePersistence(path) as store:
        for tick in range(150):
            result = run_od5a_recurring_tick(
                store, job_id=job_id, owner_id=owner,
                now=CLOCK + timedelta(seconds=tick),
                lease_duration=LEASE, trigger_id=_trigger(store, job_id),
                _wall_clock=lambda: CLOCK + timedelta(seconds=tick),
            )
            actions.append(result.action)
            if result.action == "complete":
                break
            assert result.action == "advanced"
        else:
            pytest.fail("recurring SimPy source job exceeded bounded progress")
        current = store.job_state(job_id)
        source = od5a_durable_state(store, job_id=job_id)
        assert current.status == "ready" and current.phase == "idle"
        assert current.active_trigger_id is None
        assert current.logical_time == datetime(
            2000, 1, 1, tzinfo=timezone.utc
        ) + timedelta(microseconds=round(source["clock"] * 1000000))
        assert current.next_tick == source["step"]
        assert source["done"]
        return tuple(actions)


@pytest.mark.parametrize("arm", [
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
])
def test_simpy_dispatched_recurring_physical_trace_matches_authoritative_reducer(tmp_path, arm):
    recurring = tmp_path / "recurring.db"
    reference = tmp_path / "reference.db"
    _init(recurring, arm=arm)
    with SQLitePersistence(reference) as store:
        initialize_od5a_durable(
            store, job_id="org:A", arm=_arms()[arm], items=_items(),
        )
        for _ in range(150):
            if not advance_od5a_durable(store, job_id="org:A"):
                break
        else:
            pytest.fail("reference reducer did not complete")
    actions = _drain(recurring)
    assert actions[0] == "advanced"
    assert actions[-1] == "complete"
    assert _snapshot(recurring, "org:A") == _snapshot(reference, "org:A")
    with SQLitePersistence(recurring) as store:
        assert store.simulation_position() is None  # Never invent shared global clock.
        with store.transaction() as uow:
            assert not any(
                uow.get_boundary_message(e.event_id)
                for e in od5a_durable_events(store, job_id="org:A")
            )


def test_claim_expiry_and_stale_owner_epoch_cannot_advance_physical_stage(tmp_path):
    path = tmp_path / "stale.db"
    _init(path)
    with SQLitePersistence(path) as store:
        old = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:old",
            now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
        )
        assert old.epoch == 1
        with pytest.raises(StaleOD5aRunnerClaimError):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:new",
                now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
                trigger_id=_trigger(store), recover=True,
            )
        fresh = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:new",
            now=CLOCK + timedelta(seconds=6), lease_duration=LEASE,
            trigger_id=_trigger(store), recover=True,
        )
        assert fresh.epoch == 2 and fresh.trigger_id == old.trigger_id
        before = _snapshot(path, "org:A")
        with pytest.raises(StaleOD5aRunnerClaimError):
            execute_od5a_claimed_tick(
                store, claim=old, now=CLOCK + timedelta(seconds=6),
                _wall_clock=lambda: CLOCK + timedelta(seconds=6),
            )
        assert _snapshot(path, "org:A") == before
        outcome = execute_od5a_claimed_tick(
            store, claim=fresh, now=CLOCK + timedelta(seconds=7),
            _wall_clock=lambda: CLOCK + timedelta(seconds=7),
        )
        assert outcome.action == "advanced" and outcome.epoch == fresh.epoch


def _die_after_claim(path, now):
    import os
    with SQLitePersistence(path) as store:
        claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker:dead",
            now=now, lease_duration=LEASE, trigger_id=_trigger(store),
        )
        os._exit(93)


def _die_after_source_commit(path, now):
    import os
    with SQLitePersistence(path) as store:
        run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker:dead",
            now=now, lease_duration=LEASE, trigger_id=_trigger(store),
            _wall_clock=lambda: now, lease_duration=LEASE, trigger_id=_trigger(store),
        )
        os._exit(94)


@pytest.mark.parametrize("when,code", [("claimed", 93), ("committed", 94)])
def test_real_os_process_death_recovery_preserves_exact_source_events(tmp_path, when, code):
    path = tmp_path / "interrupted.db"
    continuous = tmp_path / "continuous.db"
    _init(path)
    _init(continuous)
    ctx = get_context("spawn")
    target = _die_after_claim if when == "claimed" else _die_after_source_commit
    child = ctx.Process(target=target, args=(str(path), CLOCK))
    child.start()
    child.join(timeout=30)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("worker deadlocked")
    assert child.exitcode == code
    if when == "committed":
        # Exactly the same external occurrence is retried after the process
        # committed but its response was lost. No second physical step.
        with SQLitePersistence(path) as store:
            previously_committed = od5a_durable_events(store, job_id="org:A")
            prior_trigger = deterministic_id("od5a-recurring-tick", "org:A", 1)
            replay = run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="recovered",
                now=CLOCK + timedelta(seconds=6), lease_duration=LEASE,
                trigger_id=prior_trigger, recover=True,
                _wall_clock=lambda: CLOCK + timedelta(seconds=6), lease_duration=LEASE,
            )
            assert replay.action == "replayed"
            assert od5a_durable_events(store, job_id="org:A") == previously_committed
    if when == "claimed":
        with SQLitePersistence(path) as store:
            assert not od5a_durable_events(store, job_id="org:A")
            with pytest.raises(StaleOD5aRunnerClaimError):
                run_od5a_recurring_tick(
                    store, job_id="org:A", owner_id="recovered",
                    now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
                    trigger_id=_trigger(store), recover=True,
                    _wall_clock=lambda: CLOCK + timedelta(seconds=1), lease_duration=LEASE,
                )
            result = run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="recovered",
                now=CLOCK + timedelta(seconds=6), lease_duration=LEASE,
                trigger_id=_trigger(store), recover=True,
                _wall_clock=lambda: CLOCK + timedelta(seconds=6), lease_duration=LEASE,
            )
            assert result.action == "advanced"
    _drain(path, owner="recovered")
    _drain(continuous)
    assert _snapshot(path, "org:A") == _snapshot(continuous, "org:A")


def test_physical_event_and_job_checkpoint_rollback_together_before_commit(tmp_path, monkeypatch):
    path = tmp_path / "atomic.db"
    _init(path)
    before = _snapshot(path, "org:A")
    with SQLitePersistence(path) as store:
        original = MemoryUnitOfWork.save_job_state

        def write_and_fail(self, job):
            original(self, job)
            if job.phase == "idle" and job.last_completed_trigger_id is not None:
                raise RuntimeError("injected pre-COMMIT failure")

        with monkeypatch.context() as patch:
            patch.setattr(MemoryUnitOfWork, "save_job_state", write_and_fail)
            with pytest.raises(RuntimeError, match="pre-COMMIT"):
                run_od5a_recurring_tick(
                    store, job_id="org:A", owner_id="first",
                    now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
                    _wall_clock=lambda: CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
                )
        assert _snapshot(path, "org:A") == before
        stalled = store.job_state("org:A")
        assert stalled.active_trigger_id is not None
        assert stalled.status == "running"
        assert stalled.next_tick == 0
        after = run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="recovered",
            now=CLOCK + timedelta(seconds=6), lease_duration=LEASE,
            trigger_id=_trigger(store), recover=True,
            _wall_clock=lambda: CLOCK + timedelta(seconds=6), lease_duration=LEASE,
        )
        assert after.action == "advanced"


def test_same_completed_trigger_cannot_advance_again_after_worker_response_lost(tmp_path):
    path = tmp_path / "duplicate.db"
    _init(path)
    with SQLitePersistence(path) as store:
        first = run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker",
            now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
            _wall_clock=lambda: CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
        )
        before = _snapshot(path, "org:A")
        replay = run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker",
            now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
            trigger_id=first.trigger_id,
            _wall_clock=lambda: CLOCK + timedelta(seconds=1), lease_duration=LEASE,
        )
        assert replay.action == "replayed"
        assert _snapshot(path, "org:A") == before


def test_two_organizations_advance_local_clocks_and_fences_in_one_store(tmp_path):
    path = tmp_path / "organizations.db"
    _init(path, job_id="org:A", arm="baseline")
    _init(path, job_id="org:B", arm="structural_combined")
    with SQLitePersistence(path) as store:
        for tick in range(200):
            for job in ("org:B", "org:A"):
                result = run_od5a_recurring_tick(
                    store, job_id=job, owner_id="worker:" + job,
                    now=CLOCK + timedelta(seconds=tick),
                    lease_duration=LEASE, trigger_id=_trigger(store, job),
                    _wall_clock=lambda: CLOCK + timedelta(seconds=tick),
                )
                assert result.organization_id == job
            if all(od5a_durable_state(store, job_id=j)["done"]
                   for j in ("org:A", "org:B")):
                break
        else:
            pytest.fail("multi-organization recurrence did not terminate")
        left, right = store.job_state("org:A"), store.job_state("org:B")
        assert left.domain_name == right.domain_name == "od5a"
        assert left.bootstrap_state["pilot_identity"] != right.bootstrap_state["pilot_identity"]
        assert {x.entity_id for x in store.events() if x.name.startswith("od5a.")} == {
            "org:A", "org:B",
        }
        assert store.simulation_position() is None
        assert not store.resource_reservations()


def test_public_reference_reducer_cannot_bypass_recurring_job_fence(tmp_path):
    """A second writer cannot mutate an organization outside its runner lease."""
    path = tmp_path / "writer-bypass.db"
    _init(path)
    with SQLitePersistence(path) as store:
        with pytest.raises(RuntimeError, match="recurring.*fenced|fenced.*recurring"):
            advance_od5a_durable(store, job_id="org:A")
        assert od5a_durable_events(store, job_id="org:A") == ()
        claim = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="legitimate",
            now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
        )
        with pytest.raises(RuntimeError, match="recurring.*fenced|fenced.*recurring"):
            advance_od5a_durable(store, job_id="org:A")
        assert od5a_durable_events(store, job_id="org:A") == ()
        legitimate = execute_od5a_claimed_tick(
            store, claim=claim, now=CLOCK,
            _wall_clock=lambda: CLOCK,
        )
        assert legitimate.action == "advanced"
