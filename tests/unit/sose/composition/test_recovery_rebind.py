"""#637: fenced re-binding of a global-lease recovery job to its scoped lease (SQLite)."""
from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.cli import build_parser, run_cli
from sose.cli_recovery import cmd_composition_rebind, recovery_run_start
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.composition.recovery_rebind import (
    RecoveryRebindError,
    rebind_recovery_job_to_scope,
)
from sose.composition.recovery_trigger import scoped_config_json
from sose.composition.scheduler import RecoverySchedule
from sose.core.resource_identity import ResourcePoolContract
from sose.jobs.model import SimulationJobState
from sose.jobs.runner import scheduled_trigger_id
from sose.persistence.ownership import FencedEnginePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence, StaleWriterError

JOB = "trading-company-customer-recovery"
T0 = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
POLICY = {"composition.post_replenishment_journal": ResourcePoolContract(
    resource_type="journal_clerk", pool_id="rebind-neutral", scope="shared", capacity=1,
)}


@pytest.fixture
def passes(monkeypatch):
    """Replace the PostgreSQL-only bounded pass; record each executed occurrence."""
    executed: list[tuple[str | None, datetime]] = []

    def bounded(self, store, *, now, logical_now=None):
        executed.append((getattr(store.lease, "scope", None), logical_now))
        return 1

    monkeypatch.setattr(TradingCustomerRecoveryRunner, "_run_bounded", bounded)
    return executed


def _schedule(store, *, scoped: bool, owner: str = "worker") -> RecoverySchedule:
    runner = TradingCustomerRecoveryRunner(
        persistence=store, owner_id=owner, correlation_id="flow",
        organization_owners={"flow": "org"}, resource_policies=POLICY,
        resource_organizations={"flow": "org"}, scoped_writer=scoped,
    )
    return RecoverySchedule(
        runner=runner, start_at=T0, interval=timedelta(minutes=5), max_slots=8,
    )


def _rebind(store, **overrides):
    arguments = dict(
        job_id=JOB, owner_id="operator", correlation_id="flow",
        organization_id="org", now=T0 + timedelta(hours=1),
    )
    return rebind_recovery_job_to_scope(store, **{**arguments, **overrides})


def test_global_job_is_rebound_and_continued_by_a_scoped_runner(tmp_path, passes):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        assert len(_schedule(store, scoped=False).run_due(now=T0 + timedelta(minutes=5))) == 2
        before = store.job_state(JOB)
        assert before.config_json == "{}"

        report = _rebind(store)
        assert report.status == "rebound"
        assert report.previous_config_json == "{}"
        assert report.config_json == scoped_config_json("flow", "org")
        assert (report.run_count, report.completed_triggers) == (2, 2)
        assert report.global_writer_epoch == store.writer_epoch()

        rebound = store.job_state(JOB)
        assert rebound == replace(before, config_json=scoped_config_json("flow", "org"))

        # The same scheduled slots again: nothing re-executes.
        scoped = _schedule(store, scoped=True, owner="scoped-worker")
        assert scoped.run_due(now=T0 + timedelta(minutes=5)) == ()
        results = scoped.run_due(now=T0 + timedelta(minutes=10))
        assert [result.trigger_id for result in results] == [
            scheduled_trigger_id(JOB, T0 + timedelta(minutes=10)),
        ]
        after = store.job_state(JOB)
        assert after.job_id == JOB and after.run_count == 3
        assert [record.run_count for record in after.completed_batch_triggers] == [1, 2, 3]
        assert passes == [
            (None, T0), (None, T0 + timedelta(minutes=5)), (JOB, T0 + timedelta(minutes=10)),
        ]
        assert store.scoped_writer_epoch(JOB) == 1


def test_rebind_is_idempotent_and_claims_nothing_the_second_time(tmp_path, passes):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        _schedule(store, scoped=False).run_due(now=T0)
        first = _rebind(store)
        epoch = store.writer_epoch()
        second = _rebind(store)
        assert second.status == "already_scoped"
        assert second.global_writer_epoch is None
        assert second.config_json == first.config_json
        assert store.writer_epoch() == epoch


def test_running_global_worker_is_fenced_and_cannot_reclaim(tmp_path, passes):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        _schedule(store, scoped=False).run_due(now=T0)
        running = store.claim_writer("global-worker", expected_epoch=store.writer_epoch())
        with FencedEnginePersistence(store, running).transaction():
            pass
        _rebind(store)
        with pytest.raises(StaleWriterError, match="stale writer epoch"):
            with FencedEnginePersistence(store, running).transaction():
                pass
        with pytest.raises(ValueError, match="durable organizational binding changed"):
            _schedule(store, scoped=False).run_due(now=T0 + timedelta(minutes=5))


def test_scoped_runner_without_rebind_is_still_refused(tmp_path, passes):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        _schedule(store, scoped=False).run_due(now=T0)
        epoch = store.writer_epoch()
        with pytest.raises(ValueError, match="durable organizational binding changed"):
            _schedule(store, scoped=True).run_due(now=T0 + timedelta(minutes=5))
        assert store.job_state(JOB).config_json == "{}"
        assert store.writer_epoch() == epoch
        assert store.scoped_writer_epoch(JOB) == 0


def _register(store, *, domain="trading_company_composition", config="{}", **fields):
    state = SimulationJobState(
        job_id=JOB, domain_name=domain, config_json=config, config_revision=1,
        status="ready", initialized=True, logical_time=T0, next_tick=0,
    )
    store.claim_writer_and_register(
        "seed", expected_epoch=store.writer_epoch(), initial_job=replace(state, **fields),
    )


@pytest.mark.parametrize(("seed", "message"), [
    ({"domain": "order_to_cash"}, "not a trading-company composition recovery job"),
    ({"config": '{"correlation_id":"x","organization_id":"y"}'}, "another scoped identity"),
    ({"config": '{"tenant":"x"}'}, "another scoped identity"),
    ({"active_trigger_id": "slot", "status": "running"}, "still in flight"),
])
def test_refuses_jobs_it_cannot_rebind_without_fencing(tmp_path, seed, message):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        _register(store, **seed)
        epoch = store.writer_epoch()
        with pytest.raises(RecoveryRebindError, match=message):
            _rebind(store)
        assert store.writer_epoch() == epoch


def test_refuses_a_missing_job(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        with pytest.raises(RecoveryRebindError, match="does not exist"):
            _rebind(store)


def test_refuses_organization_mismatch_with_an_already_scoped_job(tmp_path, passes):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        _schedule(store, scoped=False).run_due(now=T0)
        _rebind(store)
        with pytest.raises(RecoveryRebindError, match="another scoped identity"):
            _rebind(store, organization_id="other-org")


@pytest.mark.parametrize(("prepare", "message"), [
    (lambda store: store.claim_scoped_writer(
        JOB, "w", expected_epoch=0, correlation_id="another-flow",
    ), "durable organizational binding changed"),
    (lambda store: store.claim_scoped_writer(
        "another-job", "w", expected_epoch=0, correlation_id="flow",
    ), "already owned by another job"),
])
def test_conflicting_correlation_reservation_rolls_back(tmp_path, passes, prepare, message):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        _schedule(store, scoped=False).run_due(now=T0)
        prepare(store)
        with pytest.raises(ValueError, match=message):
            _rebind(store)
        assert store.job_state(JOB).config_json == "{}"


@pytest.mark.parametrize(("overrides", "error"), [
    ({"owner_id": ""}, "owner_id must be nonempty"),
    ({"correlation_id": " "}, "correlation_id must be nonempty"),
    ({"now": datetime(2026, 11, 20)}, "timezone-aware"),
])
def test_rejects_invalid_arguments(tmp_path, overrides, error):
    with SQLiteIncrementalPersistence(tmp_path / "s.sqlite") as store:
        with pytest.raises(ValueError, match=error):
            _rebind(store, **overrides)


def test_requires_global_and_scoped_fencing():
    with pytest.raises(TypeError, match="global and scoped writer fencing"):
        _rebind(object())


def _args(**overrides) -> Namespace:
    argv = [
        "composition-recovery", "--postgres-dsn", "postgresql://unreachable.invalid/db",
        "--owner-id", "op", "--correlation-id", "flow", "--organization-id", "org",
    ]
    return replace_namespace(build_parser().parse_args(argv), **overrides)


def replace_namespace(args: Namespace, **overrides) -> Namespace:
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_cli_rebind_rejects_serve_before_connecting():
    with pytest.raises(ValueError, match="one-shot"):
        cmd_composition_rebind(_args(rebind_scoped=True, serve=True))


def test_cli_rebind_dispatches_without_schedule_options(capsys):
    """No --resource-policies/--start-at needed: it fails only on the unreachable DSN."""
    assert run_cli([
        "composition-recovery", "--postgres-dsn", "postgresql://unreachable.invalid/db",
        "--owner-id", "op", "--correlation-id", "flow", "--organization-id", "org",
        "--rebind-scoped", "--now", T0.isoformat(),
    ]) == 2
    assert "requires" not in capsys.readouterr().err


@pytest.mark.parametrize(("overrides", "error"), [
    ({}, "requires --resource-policies, --start-at, --interval-seconds"),
    ({"resource_policies": "p.json", "start_at": T0.isoformat(), "interval_seconds": 60,
      "serve": True, "now": T0.isoformat()}, "one-shot"),
    ({"resource_policies": "p.json", "start_at": T0.isoformat(), "interval_seconds": 60,
      "serve": True, "poll_seconds": 0}, "positive"),
    ({"resource_policies": "p.json", "start_at": "2026-11-20T12:00:00",
      "interval_seconds": 60}, "timezone-aware"),
])
def test_cli_run_preflight(overrides, error):
    with pytest.raises(ValueError, match=error):
        recovery_run_start(_args(**overrides))


def test_cli_run_preflight_returns_the_anchor():
    args = _args(resource_policies="p.json", start_at=T0.isoformat(), interval_seconds=60)
    assert recovery_run_start(args) == T0
