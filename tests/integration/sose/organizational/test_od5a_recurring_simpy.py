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
from sose.domain.entity import Entity


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


def _drain(path, *, job_id="org:A", owner="worker:A", start_tick=0):
    actions = []
    with SQLitePersistence(path) as store:
        for tick in range(start_tick, start_tick + 150):
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
        assert store.simulation_position(owner_id="org:A") is None  # No fabricated runtime clock.
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
            _wall_clock=lambda: CLOCK,
        )
        assert old.epoch == 1
        with pytest.raises(StaleOD5aRunnerClaimError):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:new",
                now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
                trigger_id=_trigger(store), recover=True,
                _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
        fresh = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:new",
            now=CLOCK + timedelta(seconds=6), lease_duration=LEASE,
            trigger_id=_trigger(store), recover=True,
            _wall_clock=lambda: CLOCK + timedelta(seconds=6),
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
            _wall_clock=lambda: now,
        )
        os._exit(93)


def _die_after_source_commit(path, now):
    import os
    with SQLitePersistence(path) as store:
        run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker:dead",
            now=now, lease_duration=LEASE, trigger_id=_trigger(store),
            _wall_clock=lambda: now,
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
                _wall_clock=lambda: CLOCK + timedelta(seconds=6),
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
                    _wall_clock=lambda: CLOCK + timedelta(seconds=1),
                )
            result = run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="recovered",
                now=CLOCK + timedelta(seconds=6), lease_duration=LEASE,
                trigger_id=_trigger(store), recover=True,
                _wall_clock=lambda: CLOCK + timedelta(seconds=6),
            )
            assert result.action == "advanced"
    # Recovery already persisted a lease at CLOCK+6; a fresh callback may
    # never rewind the trusted lease clock to CLOCK, even in synthetic tests.
    _drain(path, owner="recovered", start_tick=7)
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
                    _wall_clock=lambda: CLOCK,
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
            _wall_clock=lambda: CLOCK + timedelta(seconds=6),
        )
        assert after.action == "advanced"


def test_same_completed_trigger_cannot_advance_again_after_worker_response_lost(tmp_path):
    path = tmp_path / "duplicate.db"
    _init(path)
    with SQLitePersistence(path) as store:
        first = run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker",
            now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
            _wall_clock=lambda: CLOCK,
        )
        before = _snapshot(path, "org:A")
        replay = run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker",
            now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
            trigger_id=first.trigger_id,
            _wall_clock=lambda: CLOCK + timedelta(seconds=1),
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
        assert store.simulation_position(owner_id="org:A") is None
        assert store.simulation_position(owner_id="org:B") is None
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
            _wall_clock=lambda: CLOCK,
        )
        with pytest.raises(RuntimeError, match="recurring.*fenced|fenced.*recurring"):
            advance_od5a_durable(store, job_id="org:A")
        assert od5a_durable_events(store, job_id="org:A") == ()
        legitimate = execute_od5a_claimed_tick(
            store, claim=claim, now=CLOCK,
            _wall_clock=lambda: CLOCK,
        )
        assert legitimate.action == "advanced"


def test_simpy_callback_rechecks_lease_expiry_after_deferred_execution(tmp_path):
    """A preflight-valid claim may expire while queued behind DB contention."""
    path = tmp_path / "expiry-on-callback.db"
    _init(path)
    with SQLitePersistence(path) as store:
        claim = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="before-expiry",
            now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
            _wall_clock=lambda: CLOCK,
        )
        with pytest.raises(StaleOD5aRunnerClaimError):
            execute_od5a_claimed_tick(
                store, claim=claim, now=CLOCK + timedelta(seconds=1),
                _wall_clock=lambda: CLOCK + timedelta(seconds=6),
            )
        assert od5a_durable_events(store, job_id="org:A") == ()
        assert store.job_state("org:A").active_trigger_id == claim.trigger_id
        recovered = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="after-expiry",
            now=CLOCK + timedelta(seconds=6), lease_duration=LEASE,
            trigger_id=_trigger(store), recover=True,
            _wall_clock=lambda: CLOCK + timedelta(seconds=6),
        )
        assert recovered.epoch == claim.epoch + 1
        assert execute_od5a_claimed_tick(
            store, claim=recovered, now=CLOCK + timedelta(seconds=7),
            _wall_clock=lambda: CLOCK + timedelta(seconds=7),
        ).action == "advanced"


@pytest.mark.parametrize("tamper", ["lifecycle", "trigger"])
def test_released_or_wrong_trigger_lease_cannot_authorize_physical_step(tmp_path, tamper):
    path = tmp_path / "bad-lease.db"
    _init(path)
    with SQLitePersistence(path) as store:
        claim = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker",
            now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
            _wall_clock=lambda: CLOCK,
        )
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", "org:A")
            if tamper == "lifecycle":
                lease.state = "released"
            else:
                lease.attributes["trigger_id"] = "not-current-trigger"
            uow.save_entity(lease)
        with pytest.raises(StaleOD5aRunnerClaimError):
            execute_od5a_claimed_tick(
                store, claim=claim, now=CLOCK + timedelta(seconds=1),
                _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
        assert od5a_durable_events(store, job_id="org:A") == ()


@pytest.mark.parametrize("entrypoint", ["claim", "runner"])
def test_new_job_rejects_forged_released_lease_without_completed_history(
    tmp_path, entrypoint,
):
    """A released epoch before the first tick cannot be adopted as causal truth."""
    path = tmp_path / f"impossible-first-release-{entrypoint}.db"
    _init(path)
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            lease = Entity(
                id="org:A", entity_type="od5a_runner_lease",
                state="released",
                attributes={
                    "owner_id": None, "epoch": 1,
                    "trigger_id": None,
                    "expires_at": (CLOCK + LEASE).isoformat(),
                },
            )
            lease.touch(CLOCK)
            uow.save_entity(lease)

        def claim_or_run():
            kwargs = dict(
                job_id="org:A", owner_id="worker:new",
                now=CLOCK + timedelta(seconds=1),
                lease_duration=LEASE, trigger_id=_trigger(store),
                _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
            if entrypoint == "claim":
                return claim_od5a_recurring_tick(store, **kwargs)
            return run_od5a_recurring_tick(store, **kwargs)

        with pytest.raises(ValueError, match="released lease requires completed trigger history"):
            claim_or_run()
        with store.transaction() as uow:
            saved = uow.get_entity("od5a_runner_lease", "org:A")
            job = uow.get_job_state("org:A")
            assert saved.state == "released"
            assert saved.attributes["epoch"] == 1
            assert saved.attributes["trigger_id"] is None
            assert job.run_count == 0
            assert job.last_completed_trigger_id is None
            assert job.active_trigger_id is None
        assert od5a_durable_events(store, job_id="org:A") == ()


@pytest.mark.parametrize("forged_trigger", [None, "alien-trigger"])
def test_released_lease_trigger_must_match_completed_job_before_reclaim(tmp_path, forged_trigger):
    path = tmp_path / "released-trigger-corruption.db"
    _init(path)
    with SQLitePersistence(path) as store:
        first = _trigger(store)
        result = run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker:A",
            now=CLOCK, lease_duration=LEASE, trigger_id=first,
            _wall_clock=lambda: CLOCK,
        )
        assert result.action == "advanced"
        before = od5a_durable_events(store, job_id="org:A")
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", "org:A")
            assert lease.state == "released"
            lease.attributes["trigger_id"] = forged_trigger
            uow.save_entity(lease)
        with pytest.raises(ValueError, match="completed trigger lacks released lease history"):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="worker:B",
                now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
                trigger_id=_trigger(store),
                _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
        assert od5a_durable_events(store, job_id="org:A") == before


@pytest.mark.parametrize("completed_ticks,forged_run_count", [
    (1, True), (2, 2.0),
])
def test_checkpoint_rejects_bool_or_float_run_count(
    tmp_path, completed_ticks, forged_run_count,
):
    """Numeric equality is not sufficient to admit authoritative job history."""
    from dataclasses import replace

    path = tmp_path / "forged-count-type.db"
    _init(path)
    with SQLitePersistence(path) as store:
        for second in range(completed_ticks):
            run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:A",
                now=CLOCK + timedelta(seconds=second),
                lease_duration=LEASE, trigger_id=_trigger(store),
                _wall_clock=lambda second=second: CLOCK + timedelta(seconds=second),
            )
        before = od5a_durable_events(store, job_id="org:A")
        with store.transaction() as uow:
            job = uow.get_job_state("org:A")
            assert job.run_count == completed_ticks
            uow.save_job_state(replace(job, run_count=forged_run_count))
        with pytest.raises(ValueError, match="checkpoint contradicts authoritative organization"):
            run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:B",
                now=CLOCK + timedelta(seconds=completed_ticks),
                lease_duration=LEASE, trigger_id=deterministic_id(
                    "od5a-recurring-tick", "org:A", completed_ticks + 1,
                ),
                _wall_clock=lambda: CLOCK + timedelta(seconds=completed_ticks),
            )
        assert od5a_durable_events(store, job_id="org:A") == before
        assert type(store.job_state("org:A").run_count) is type(forged_run_count)


@pytest.mark.parametrize("malformed_epoch", [True, 1.0])
def test_callback_refuses_noninteger_claimed_epoch(tmp_path, malformed_epoch):
    """Python numeric equality must never authorize a corrupt physical fence."""
    path = tmp_path / "noninteger-fenced-epoch.db"
    _init(path)
    with SQLitePersistence(path) as store:
        claimed = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:A",
            now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
            _wall_clock=lambda: CLOCK,
        )
        before = od5a_durable_events(store, job_id="org:A")
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", "org:A")
            lease.attributes["epoch"] = malformed_epoch
            uow.save_entity(lease)
        with pytest.raises(ValueError, match="invalid durable OD-5a runner epoch"):
            execute_od5a_claimed_tick(
                store, claim=claimed, now=CLOCK + timedelta(seconds=1),
                _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
        assert od5a_durable_events(store, job_id="org:A") == before
        assert store.job_state("org:A").run_count == 0


@pytest.mark.parametrize("corrupt_count", [1, 3])
def test_checkpoint_run_count_cannot_diverge_from_source_step(tmp_path, corrupt_count):
    """Scheduled occurrence numbering is bound to durable physical progress."""
    from dataclasses import replace

    path = tmp_path / "regressed-run-count.db"
    _init(path)
    with SQLitePersistence(path) as store:
        for second in (0, 1):
            run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:A",
                now=CLOCK + timedelta(seconds=second),
                lease_duration=LEASE, trigger_id=_trigger(store),
                _wall_clock=lambda second=second: CLOCK + timedelta(seconds=second),
            )
        claim = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:A",
            now=CLOCK + timedelta(seconds=2), lease_duration=LEASE,
            trigger_id=_trigger(store),
            _wall_clock=lambda: CLOCK + timedelta(seconds=2),
        )
        before = od5a_durable_events(store, job_id="org:A")
        with store.transaction() as uow:
            job = uow.get_job_state("org:A")
            assert job.next_tick == job.run_count == 2
            uow.save_job_state(replace(job, run_count=corrupt_count))
        with pytest.raises(ValueError, match="checkpoint contradicts authoritative organization"):
            execute_od5a_claimed_tick(
                store, claim=claim, now=CLOCK + timedelta(seconds=3),
                _wall_clock=lambda: CLOCK + timedelta(seconds=3),
            )
        assert od5a_durable_events(store, job_id="org:A") == before
        assert store.job_state("org:A").run_count == corrupt_count


@pytest.mark.parametrize("entrypoint", ["execute", "reclaim"])
def test_claimed_epoch_cannot_regress_below_next_run_count(tmp_path, entrypoint):
    """An active lease cannot lose its committed epoch history."""
    from dataclasses import replace

    path = tmp_path / f"regressed-active-epoch-{entrypoint}.db"
    _init(path)
    with SQLitePersistence(path) as store:
        for second in (0, 1):
            run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:A",
                now=CLOCK + timedelta(seconds=second),
                lease_duration=LEASE, trigger_id=_trigger(store),
                _wall_clock=lambda second=second: CLOCK + timedelta(seconds=second),
            )
        assert store.job_state("org:A").run_count == 2
        trigger = _trigger(store)
        claimed = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:A",
            now=CLOCK + timedelta(seconds=2), lease_duration=LEASE,
            trigger_id=trigger,
            _wall_clock=lambda: CLOCK + timedelta(seconds=2),
        )
        assert claimed.epoch == 3
        before = od5a_durable_events(store, job_id="org:A")
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", "org:A")
            assert lease.state == "claimed"
            lease.attributes["epoch"] = 1
            uow.save_entity(lease)

        with pytest.raises(ValueError, match="claimed lease epoch predates next run"):
            if entrypoint == "execute":
                execute_od5a_claimed_tick(
                    store, claim=replace(claimed, epoch=1),
                    now=CLOCK + timedelta(seconds=3),
                    _wall_clock=lambda: CLOCK + timedelta(seconds=3),
                )
            else:
                claim_od5a_recurring_tick(
                    store, job_id="org:A", owner_id="owner:B",
                    now=CLOCK + timedelta(seconds=9),
                    lease_duration=LEASE, trigger_id=trigger, recover=True,
                    _wall_clock=lambda: CLOCK + timedelta(seconds=9),
                )
        assert od5a_durable_events(store, job_id="org:A") == before
        assert store.job_state("org:A").run_count == 2
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", "org:A")
            assert lease.state == "claimed" and lease.attributes["epoch"] == 1


@pytest.mark.parametrize("entrypoint", ["replay", "claim"])
def test_released_epoch_cannot_regress_below_completed_run_count(tmp_path, entrypoint):
    """A completed event history cannot be laundered through a smaller epoch."""
    path = tmp_path / f"regressed-release-epoch-{entrypoint}.db"
    _init(path)
    with SQLitePersistence(path) as store:
        completed = None
        for second in (0, 1):
            completed = _trigger(store)
            run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:A",
                now=CLOCK + timedelta(seconds=second),
                lease_duration=LEASE, trigger_id=completed,
                _wall_clock=lambda second=second: CLOCK + timedelta(seconds=second),
            )
        checkpoint = store.job_state("org:A")
        assert checkpoint.run_count == 2
        before = od5a_durable_events(store, job_id="org:A")
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", "org:A")
            assert lease.state == "released" and lease.attributes["epoch"] == 2
            lease.attributes["epoch"] = 1
            uow.save_entity(lease)
        with pytest.raises(ValueError, match="completed trigger lacks released lease history"):
            trigger = completed if entrypoint == "replay" else _trigger(store)
            run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:B",
                now=CLOCK + timedelta(seconds=2),
                lease_duration=LEASE, trigger_id=trigger,
                _wall_clock=lambda: CLOCK + timedelta(seconds=2),
            )
        assert od5a_durable_events(store, job_id="org:A") == before
        assert store.job_state("org:A").run_count == 2


@pytest.mark.parametrize("tamper", [
    {"status": "failed"}, {"phase": "reconcile"},
])
def test_completed_checkpoint_lifecycle_cannot_replay_or_reclaim(tmp_path, tamper):
    from dataclasses import replace

    path = tmp_path / "completed-checkpoint-corrupt.db"
    _init(path)
    with SQLitePersistence(path) as store:
        completed_trigger = _trigger(store)
        first = run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:A",
            now=CLOCK, lease_duration=LEASE, trigger_id=completed_trigger,
            _wall_clock=lambda: CLOCK,
        )
        assert first.action == "advanced"
        before = od5a_durable_events(store, job_id="org:A")
        with store.transaction() as uow:
            checkpoint = uow.get_job_state("org:A")
            assert checkpoint.last_completed_trigger_id == completed_trigger
            assert checkpoint.active_trigger_id is None
            uow.save_job_state(replace(checkpoint, **tamper))
        with pytest.raises(ValueError, match="completed checkpoint lifecycle"):
            run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:B",
                now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
                trigger_id=completed_trigger,
                _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
        with pytest.raises(ValueError, match="completed checkpoint lifecycle"):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:B",
                now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
                trigger_id=_trigger(store),
                _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
        assert od5a_durable_events(store, job_id="org:A") == before


def test_future_dated_worker_clock_never_steals_live_claim_or_expires_callback(tmp_path):
    """The caller's 'now' is not evidence of PostgreSQL/SQLite lease time."""
    path = tmp_path / "host-skew.db"
    _init(path)
    with SQLitePersistence(path) as store:
        trigger = _trigger(store)
        original = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:A", now=CLOCK,
            lease_duration=LEASE, trigger_id=trigger,
            _wall_clock=lambda: CLOCK,
        )
        # This future worker-local clock must NEVER grant a fenced takeover.
        future = CLOCK + timedelta(days=100)
        with pytest.raises(StaleOD5aRunnerClaimError):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:B",
                now=future, lease_duration=LEASE, trigger_id=trigger,
                recover=True, _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
        # A preflight-skewed invocation cannot force early expiration either.
        legitimate = execute_od5a_claimed_tick(
            store, claim=original, now=future,
            _wall_clock=lambda: CLOCK + timedelta(seconds=2),
        )
        assert legitimate.action == "advanced"
        assert store.job_state("org:A").last_completed_trigger_id == trigger
        assert len(od5a_durable_events(store, job_id="org:A")) == 1


# Adversarial coverage gate: every durable guard below is a business-level
# fail-closed contract. These are not pragma exclusions or coverage cosmetics.
@pytest.mark.parametrize("tamper", [
    {"domain_name": "cross-domain"},
    {"config_json": "{}"},
    {"bootstrap_state": {"pilot_identity": "forged"}},
    {"initialized": False},
    {"config_revision": 2},
    {"next_tick": 99},
    {"logical_time": CLOCK},
])
def test_recurring_rejects_divergent_authoritative_checkpoint(tmp_path, tamper):
    from dataclasses import replace

    path = tmp_path / "checkpoint-tamper.db"
    _init(path)
    with SQLitePersistence(path) as store:
        before = od5a_durable_events(store, job_id="org:A")
        if "domain_name" in tamper:
            # The authoritative UoW rejects changing the job domain before
            # a contradictory checkpoint can reach the recurring reader.
            with pytest.raises(ValueError, match="job domain cannot change"):
                with store.transaction() as uow:
                    state = uow.get_job_state("org:A")
                    uow.save_job_state(replace(state, **tamper))
            assert od5a_durable_events(store, job_id="org:A") == before
            return
        with store.transaction() as uow:
            state = uow.get_job_state("org:A")
            uow.save_job_state(replace(state, **tamper))
        with pytest.raises(ValueError, match="checkpoint contradicts"):
            run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="worker",
                now=CLOCK, lease_duration=LEASE,
                trigger_id=deterministic_id("od5a-recurring-tick", "org:A", 1),
                _wall_clock=lambda: CLOCK,
            )
        assert od5a_durable_events(store, job_id="org:A") == before


@pytest.mark.parametrize("bad_owner,bad_trigger,bad_time,bad_lease,reason", [
    ("", None, CLOCK, LEASE, "stable trigger ID must be nonempty"),
    ("owner", "", CLOCK, LEASE, "stable trigger ID must be nonempty"),
    ("owner", None, CLOCK.replace(tzinfo=None), LEASE, "timezone-aware"),
    ("owner", None, CLOCK, timedelta(0), "lease must be positive"),
    ("owner", None, CLOCK, timedelta(seconds=-1), "lease must be positive"),
])
def test_claim_validates_external_trigger_worker_and_lease(
    tmp_path, bad_owner, bad_trigger, bad_time, bad_lease, reason,
):
    path = tmp_path / "invalid-claim-input.db"
    _init(path)
    with SQLitePersistence(path) as store:
        # Keep every other input valid, so removing the intended guard
        # cannot still produce a false-green ValueError downstream.
        trigger = _trigger(store) if bad_trigger is None else bad_trigger
        with pytest.raises(ValueError, match=reason):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id=bad_owner,
                now=bad_time, lease_duration=bad_lease, trigger_id=trigger,
                _wall_clock=lambda: CLOCK,
            )
        assert od5a_durable_events(store, job_id="org:A") == ()


@pytest.mark.parametrize("mutator,reason", [
    ("epoch_zero", "invalid durable"),
    ("epoch_bool", "invalid durable"),
    ("expiry_missing", "invalid expiry"),
    ("invalid_state", "invalid durable lifecycle"),
    ("owner_missing", "claimed without an owner"),
    ("released_owned", "owner contradicts released"),
    ("wrong_active_trigger", "lease trigger contradicts active job"),
    ("expiry_before_claim", "lease expiry contradicts claim timestamp"),
    ("owner_empty", "claimed without an owner"),
    ("owner_integer", "claimed without an owner"),
    ("job_ready", "claimed lease contradicts job lifecycle"),
    ("job_idle", "claimed lease contradicts job lifecycle"),
])
def test_corrupt_fenced_lease_fails_closed_before_reclaim(tmp_path, mutator, reason):
    path = tmp_path / "forged-lease.db"
    _init(path)
    with SQLitePersistence(path) as store:
        trigger = _trigger(store)
        claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:A", now=CLOCK,
            lease_duration=LEASE, trigger_id=trigger,
            _wall_clock=lambda: CLOCK,
        )
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", "org:A")
            if mutator == "epoch_zero":
                lease.attributes["epoch"] = 0
            elif mutator == "epoch_bool":
                lease.attributes["epoch"] = True
            elif mutator == "expiry_missing":
                lease.attributes["expires_at"] = None
            elif mutator == "invalid_state":
                lease.state = "invented"
            elif mutator == "owner_missing":
                lease.attributes["owner_id"] = None
            elif mutator == "released_owned":
                lease.state = "released"
            elif mutator == "wrong_active_trigger":
                lease.attributes["trigger_id"] = "foreign-stale-trigger"
            elif mutator == "expiry_before_claim":
                lease.attributes["expires_at"] = lease.updated_at.isoformat()
            elif mutator == "owner_empty":
                lease.attributes["owner_id"] = ""
            elif mutator == "owner_integer":
                lease.attributes["owner_id"] = 17
            elif mutator in {"job_ready", "job_idle"}:
                from dataclasses import replace
                job = uow.get_job_state("org:A")
                if mutator == "job_ready":
                    uow.save_job_state(replace(job, status="ready"))
                else:
                    uow.save_job_state(replace(job, phase="idle"))
            uow.save_entity(lease)
        with pytest.raises(ValueError, match=reason):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="owner:B",
                now=CLOCK + timedelta(seconds=10),
                lease_duration=LEASE, trigger_id=trigger, recover=True,
                _wall_clock=lambda: CLOCK + timedelta(seconds=10),
            )
        assert od5a_durable_events(store, job_id="org:A") == ()


def test_missing_released_lease_cannot_replay_or_advance_completed_trigger(tmp_path):
    path = tmp_path / "missing-causal-fence.db"
    _init(path)
    with SQLitePersistence(path) as store:
        first_trigger = _trigger(store)
        first = run_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker:old",
            now=CLOCK, lease_duration=LEASE, trigger_id=first_trigger,
            _wall_clock=lambda: CLOCK,
        )
        assert first.action == "advanced"
        previous_events = od5a_durable_events(store, job_id="org:A")
        with store.transaction() as uow:
            key = ("od5a_runner_lease", "org:A")
            # Deliberately corrupt storage, bypassing public Entity APIs.
            del uow._working.entities[key]
            uow._mark_dirty("entities", key)
        with pytest.raises(ValueError, match="completed trigger lacks released lease"):
            run_od5a_recurring_tick(
                store, job_id="org:A", owner_id="worker:new",
                now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
                trigger_id=first_trigger,
                _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
        with pytest.raises(ValueError, match="completed trigger lacks released lease"):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="worker:new",
                now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
                trigger_id=_trigger(store),
                _wall_clock=lambda: CLOCK + timedelta(seconds=1),
            )
        assert od5a_durable_events(store, job_id="org:A") == previous_events


def test_same_owner_can_reclaim_current_live_epoch_without_mutation(tmp_path):
    path = tmp_path / "same-owner.db"
    _init(path)
    with SQLitePersistence(path) as store:
        trigger = _trigger(store)
        old = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:A",
            now=CLOCK, lease_duration=LEASE, trigger_id=trigger,
            _wall_clock=lambda: CLOCK,
        )
        again = claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="owner:A",
            now=CLOCK + timedelta(seconds=1), lease_duration=LEASE,
            trigger_id=trigger, _wall_clock=lambda: CLOCK + timedelta(seconds=1),
        )
        assert again == old
        assert store.job_state("org:A").next_tick == 0
        assert od5a_durable_events(store, job_id="org:A") == ()


def test_untracked_standalone_history_cannot_be_bound_to_new_recurring_job(tmp_path):
    path = tmp_path / "untracked-source.db"
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(
            store, job_id="org:A", arm=_arms()["baseline"], items=_items(),
        )
        assert advance_od5a_durable(store, job_id="org:A")
        prior = od5a_durable_events(store, job_id="org:A")
        with pytest.raises(ValueError, match="untracked physical history"):
            initialize_od5a_recurring_job(
                store, job_id="org:A",
                arm=_arms()["baseline"], items=_items(),
            )
        assert store.job_state("org:A") is None
        assert od5a_durable_events(store, job_id="org:A") == prior


def test_recurring_registration_is_idempotent_and_immutably_bound(tmp_path):
    path = tmp_path / "register-twice.db"
    _init(path)
    with SQLitePersistence(path) as store:
        first = store.job_state("org:A")
        repeat = initialize_od5a_recurring_job(
            store, job_id="org:A", arm=_arms()["baseline"], items=_items(),
        )
        assert repeat == first
        assert store.job_state("org:A") == first
        with pytest.raises(ValueError, match="identity conflicts"):
            initialize_od5a_recurring_job(
                store, job_id="org:A", arm=_arms()["capacity_only"],
                items=_items(),
            )


def test_fenced_claim_does_not_accept_untrusted_naive_wall_time(tmp_path):
    path = tmp_path / "invalid-clock.db"
    _init(path)
    with SQLitePersistence(path) as store:
        with pytest.raises(ValueError, match="timezone-aware"):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="worker",
                now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
                _wall_clock=lambda: CLOCK.replace(tzinfo=None),
            )
        assert od5a_durable_events(store, job_id="org:A") == ()


def test_claim_denies_wrong_trigger_paused_checkpoint_and_expired_reclaim_without_opt_in(tmp_path):
    from dataclasses import replace

    path = tmp_path / "claim-gates.db"
    _init(path)
    with SQLitePersistence(path) as store:
        with pytest.raises(ValueError, match="next deterministic step"):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="worker",
                now=CLOCK, lease_duration=LEASE, trigger_id="wrong",
                _wall_clock=lambda: CLOCK,
            )
        with store.transaction() as uow:
            job = uow.get_job_state("org:A")
            uow.save_job_state(replace(job, status="paused"))
        with pytest.raises(ValueError, match="paused"):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="worker",
                now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
                _wall_clock=lambda: CLOCK,
            )
        with store.transaction() as uow:
            job = uow.get_job_state("org:A")
            uow.save_job_state(replace(job, status="ready"))
        trigger = _trigger(store)
        claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker:A",
            now=CLOCK, lease_duration=LEASE, trigger_id=trigger,
            _wall_clock=lambda: CLOCK,
        )
        with pytest.raises(StaleOD5aRunnerClaimError, match="explicit recovery"):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="worker:B",
                now=CLOCK + timedelta(seconds=6), lease_duration=LEASE,
                trigger_id=trigger, recover=False,
                _wall_clock=lambda: CLOCK + timedelta(seconds=6),
            )
        assert od5a_durable_events(store, job_id="org:A") == ()


@pytest.mark.parametrize("corruption,error", [
    ("missing_active", "matching lease"),
    ("wrong_reservation", "truth differs"),
    ("deleted_reservation", "disappeared before completion"),
    ("regressed_clock", "clock regressed"),
    ("unknown_action", "unknown scheduled durable action"),
    ("empty_actions_pending", "pending physical allocations"),
    ("empty_actions_unfinished", "without completing every item"),
])
def test_source_reducer_refuses_corrupt_physical_and_causal_recovery(tmp_path, corruption, error):
    """The durable reducer must reject forged physical facts atomically."""
    path = tmp_path / "bad-physical-world.db"
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(
            store, job_id="org:A", arm=_arms()["baseline"], items=_items(),
        )
        if corruption not in ("empty_actions_pending", "empty_actions_unfinished"):
            assert advance_od5a_durable(store, job_id="org:A")
        with store.transaction() as uow:
            source = uow.get_entity("od5a_durable_pilot", "org:A")
            state = source.attributes
            if corruption == "missing_active":
                state["active"].pop("execution:0")
            elif corruption == "wrong_reservation":
                state["resources"]["execution:0"] = "foreign-reservation"
            elif corruption == "deleted_reservation":
                reservation_id = state["active"]["execution:0"]["reservation_id"]
                uow.delete_resource_reservation(reservation_id)
            elif corruption == "regressed_clock":
                state["clock"] = 100.0
            elif corruption == "unknown_action":
                next_action = min(state["actions"], key=lambda x: (x["at"], x["seq"]))
                next_action["kind"] = "phantom_action"
            elif corruption == "empty_actions_pending":
                state["actions"] = []
                state["pending_execution"] = [{
                    "item": "item:A", "source": "a", "target": "a", "causes": [],
                }]
            elif corruption == "empty_actions_unfinished":
                state["actions"] = []
            uow.save_entity(source)
        before_events = od5a_durable_events(store, job_id="org:A")
        before_source = od5a_durable_state(store, job_id="org:A")
        before_reservations = store.resource_reservations()
        with pytest.raises(RuntimeError, match=error):
            advance_od5a_durable(store, job_id="org:A")
        assert od5a_durable_events(store, job_id="org:A") == before_events
        assert od5a_durable_state(store, job_id="org:A") == before_source
        assert store.resource_reservations() == before_reservations


@pytest.mark.parametrize("conflict,error", [
    ("duplicate_active", "double allocation"),
    ("external_reservation", "capacity exhausted"),
    ("repeated_stage", "stage requested twice"),
])
def test_source_grant_and_stage_identity_are_authoritative(tmp_path, conflict, error):
    """A committed arrival must not grant on top of a foreign physical lease."""
    from sose.core.runtime import ResourceReservation

    path = tmp_path / "foreign-physical-lease.db"
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(
            store, job_id="org:A", arm=_arms()["baseline"], items=_items(),
        )
        with store.transaction() as uow:
            source = uow.get_entity("od5a_durable_pilot", "org:A")
            if conflict == "duplicate_active":
                source.attributes["active"]["execution:0"] = {
                    "kind": "execution", "reservation_id": "external",
                }
            elif conflict == "external_reservation":
                uow.save_resource_reservation(ResourceReservation(
                    reservation_id="external", request_id="foreign-request",
                    resource_name="org:A/execution:0",
                    acquired_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
                    sequence=0,
                ))
            else:
                source.attributes["requested"].append(
                    deterministic_id("od5a-durable-stage", "item:A", "a")
                )
            uow.save_entity(source)
        initial_events = od5a_durable_events(store, job_id="org:A")
        initial_state = od5a_durable_state(store, job_id="org:A")
        initial_reservations = store.resource_reservations()
        with pytest.raises(RuntimeError, match=error):
            advance_od5a_durable(store, job_id="org:A")
        assert od5a_durable_events(store, job_id="org:A") == initial_events
        assert od5a_durable_state(store, job_id="org:A") == initial_state
        assert store.resource_reservations() == initial_reservations


@pytest.mark.parametrize("invalid_items", [
    (),
    (_items()[0], _items()[0]),
    (OD5aWorkItem("bad", 0.0, (("a", 0.1), ("b", 0.2))),),
])
def test_source_workload_rejects_missing_duplicate_and_incomplete_identity(tmp_path, invalid_items):
    path = tmp_path / "invalid-pilot.db"
    with SQLitePersistence(path) as store:
        with pytest.raises(ValueError):
            initialize_od5a_durable(
                store, job_id="org:A", arm=_arms()["baseline"], items=invalid_items,
            )
        assert store.events() == ()
        assert store.entity("od5a_durable_pilot", "org:A") is None


def test_source_unknown_org_rejected_without_creating_synthetic_events(tmp_path):
    with SQLitePersistence(tmp_path / "unknown-reducer.db") as store:
        with pytest.raises(KeyError):
            advance_od5a_durable(store, job_id="unknown:org")
        assert store.events() == ()


@pytest.mark.parametrize("forgery,reason", [
    ("wrong_trigger", "lease trigger contradicts"),
    ("expiry_not_after_claim", "lease expiry contradicts"),
])
def test_expired_claim_cannot_launder_forged_authoritative_lease(tmp_path, forgery, reason):
    """Reclaim must reject corruption BEFORE writing a fresh epoch over it."""
    path = tmp_path / "forged-reclaim.db"
    _init(path)
    with SQLitePersistence(path) as store:
        trigger = _trigger(store)
        claim_od5a_recurring_tick(
            store, job_id="org:A", owner_id="worker:old", now=CLOCK,
            lease_duration=LEASE, trigger_id=trigger,
            _wall_clock=lambda: CLOCK,
        )
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", "org:A")
            if forgery == "wrong_trigger":
                lease.attributes["trigger_id"] = "forged-event"
            else:
                lease.attributes["expires_at"] = lease.updated_at.isoformat()
            uow.save_entity(lease)
        before = od5a_durable_state(store, job_id="org:A")
        with pytest.raises(ValueError, match=reason):
            claim_od5a_recurring_tick(
                store, job_id="org:A", owner_id="worker:new",
                now=CLOCK + timedelta(seconds=10),
                lease_duration=LEASE, trigger_id=trigger, recover=True,
                _wall_clock=lambda: CLOCK + timedelta(seconds=10),
            )
        assert od5a_durable_state(store, job_id="org:A") == before
        assert store.job_state("org:A").active_trigger_id == trigger
        assert od5a_durable_events(store, job_id="org:A") == ()


def test_foreign_domain_job_collision_rolls_back_source_and_resources(tmp_path):
    from sose.jobs.model import SimulationJobState

    path = tmp_path / "foreign-job.db"
    with SQLitePersistence(path) as store:
        foreign = SimulationJobState(
            job_id="org:A", domain_name="foreign-domain",
            config_json="{}", config_revision=1, status="ready",
            initialized=True, logical_time=CLOCK, next_tick=0,
        )
        with store.transaction() as uow:
            uow.save_job_state(foreign)
        with pytest.raises(ValueError, match="another domain"):
            initialize_od5a_recurring_job(
                store, job_id="org:A", arm=_arms()["baseline"], items=_items(),
            )
        assert store.job_state("org:A") == foreign
        assert store.entity("od5a_durable_pilot", "org:A") is None
        assert store.resource_definitions() == ()
        assert store.events() == ()


def test_registration_failure_rolls_back_source_in_same_authoritative_uow(tmp_path, monkeypatch):
    """Physical source creation and owner SimulationJobState are one commit."""
    path = tmp_path / "atomic-register.db"
    original = MemoryUnitOfWork.save_job_state

    def fail_once(self, state):
        if state.job_id == "org:A" and state.domain_name == "od5a":
            raise RuntimeError("registration fault before COMMIT")
        return original(self, state)

    with SQLitePersistence(path) as store:
        with monkeypatch.context() as patch:
            patch.setattr(MemoryUnitOfWork, "save_job_state", fail_once)
            with pytest.raises(RuntimeError, match="registration fault"):
                initialize_od5a_recurring_job(
                    store, job_id="org:A", arm=_arms()["baseline"], items=_items(),
                )
        assert store.job_state("org:A") is None
        assert store.entity("od5a_durable_pilot", "org:A") is None
        assert store.resource_definitions() == ()
        assert store.events() == ()
        initialize_od5a_recurring_job(
            store, job_id="org:A", arm=_arms()["baseline"], items=_items(),
        )
        assert store.job_state("org:A") is not None


def test_full_scheduled_action_identity_is_rechecked_inside_simpy_callback(
    tmp_path, monkeypatch,
):
    """Same time/sequence/kind but another item must not be dispatched."""
    from sose.backends.simpy import SimPyBackend

    path = tmp_path / "causal-swap.db"
    _init(path)
    original_run_until = SimPyBackend.run_until
    injected = []

    def swap_after_preflight(self, end):
        if not injected:
            with SQLitePersistence(path) as writer:
                with writer.transaction() as uow:
                    source = uow.get_entity("od5a_durable_pilot", "org:A")
                    action = min(
                        source.attributes["actions"],
                        key=lambda item: (item["at"], item["seq"]),
                    )
                    assert action["kind"] == "arrival"
                    action["item"] = "item:B"
                    uow.save_entity(source)
            injected.append(True)
        return original_run_until(self, end)

    with SQLitePersistence(path) as store:
        with monkeypatch.context() as patch:
            patch.setattr(SimPyBackend, "run_until", swap_after_preflight)
            with pytest.raises(StaleOD5aRunnerClaimError, match="scheduled step changed"):
                run_od5a_recurring_tick(
                    store, job_id="org:A", owner_id="worker",
                    now=CLOCK, lease_duration=LEASE, trigger_id=_trigger(store),
                    _wall_clock=lambda: CLOCK,
                )
        assert injected == [True]
        assert od5a_durable_events(store, job_id="org:A") == ()
        assert store.job_state("org:A").next_tick == 0
        assert store.job_state("org:A").active_trigger_id is not None
