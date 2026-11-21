"""#637: ``composition-recovery --rebind-scoped`` moves a global-lease job to its scoped lease.

The global lease was removed in 0.14.0 (#635); ``seed`` writes the job a 0.13 global
run left behind, which is what ``--rebind-scoped`` migrates.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from psycopg import sql

from sose.cli import run_cli
from sose.composition.recovery_rebind import (
    RecoveryRebindError,
    rebind_recovery_job_to_scope,
)
from sose.composition.recovery_trigger import scoped_config_json
from sose.core.resource_identity import ResourcePoolContract
from sose.jobs.runner import scheduled_trigger_id
from sose.persistence.ownership import FencedEnginePersistence
from tests.support.global_lease_job import seed_global_lease_job

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
JOB = "trading-company-customer-recovery"
START = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
POLICIES = {"composition.deliver_shipment": ResourcePoolContract(
    resource_type="pickup_courier", pool_id="rebind-couriers", scope="shared", capacity=1,
)}


@pytest.fixture
def cli(tmp_path, capsys):
    namespace = f"rebind_{uuid4().hex[:12]}"
    policies = tmp_path / "policies.json"
    policies.write_text(json.dumps({
        "composition.deliver_shipment": {
            "resource_type": "pickup_courier", "pool_id": "rebind-couriers",
            "scope": "shared", "capacity": 1,
        },
    }), encoding="utf-8")
    common = [
        "--postgres-dsn", DSN, "--namespace", namespace, "--owner-id", "cron",
        "--correlation-id", "flow", "--organization-id", "org",
    ]

    def run(*extra: str, minutes: int = 1) -> int:
        return run_cli([
            "composition-recovery", *common, "--resource-policies", str(policies),
            "--start-at", START.isoformat(), "--interval-seconds", "60",
            "--now", (START + timedelta(minutes=minutes)).isoformat(), *extra,
        ])

    def rebind() -> tuple[int, dict | None]:
        capsys.readouterr()
        code = run_cli(["composition-recovery", *common, "--rebind-scoped"])
        out = capsys.readouterr().out.strip()
        return code, (json.loads(out) if out else None)

    def seed(minutes: int = 1) -> None:
        """A 0.13 global-lease worker ran the slots up to ``START + minutes``."""
        with _store(namespace) as store:
            slots = [START + timedelta(minutes=m) for m in range(minutes + 1)]
            assert seed_global_lease_job(
                store, slots, owner_id="cron", resource_policies=POLICIES,
            ) == len(slots)

    run.rebind = rebind
    run.seed = seed
    run.namespace = namespace
    run.capsys = capsys
    return run


def _store(namespace):
    from sose.persistence.postgres import PostgresPersistence

    return PostgresPersistence(DSN, namespace=namespace)


def test_cli_rebinds_global_job_and_scoped_worker_continues(cli):
    cli.seed()                                           # 0.13 global lease: slots 0 and 1
    with _store(cli.namespace) as store:
        before = store.job_state(JOB)
    assert (before.config_json, before.run_count) == ("{}", 2)

    with _store(cli.namespace) as store:
        epochs = (store.writer_epoch(), store.scoped_writer_epoch(JOB))
    assert cli(minutes=2) == 2                           # refused until re-bound
    assert "re-bind it once with sose composition-recovery --rebind-scoped" in (
        cli.capsys.readouterr().err
    )
    with _store(cli.namespace) as store:                 # refused before any claim
        assert (store.writer_epoch(), store.scoped_writer_epoch(JOB)) == epochs
        assert epochs[1] == 0

    code, report = cli.rebind()
    assert code == 0 and report["status"] == "rebound"
    assert report["config_json"] == scoped_config_json("flow", "org")
    assert (report["run_count"], report["completed_triggers"]) == (2, 2)
    code, again = cli.rebind()
    assert code == 0 and again["status"] == "already_scoped"
    assert again["global_writer_epoch"] is None

    with pytest.raises(ValueError, match="durable organizational binding changed"):
        cli.seed(minutes=2)                              # a restarted 0.13 worker is refused
    assert cli() == 0                                    # same slots: no new occurrence
    assert cli("--scoped-writer", minutes=2) == 0
    assert cli("--scoped-writer", minutes=2) == 0
    with _store(cli.namespace) as store:
        after = store.job_state(JOB)
        assert store.scoped_writer_epoch(JOB) == 1
    assert after.job_id == JOB and after.run_count == 3
    assert after.completed_batch_triggers[:2] == before.completed_batch_triggers
    assert after.last_completed_trigger_id == scheduled_trigger_id(
        JOB, START + timedelta(minutes=2),
    )


def _rebind(store, **overrides):
    arguments = dict(
        job_id=JOB, owner_id="operator", correlation_id="flow",
        organization_id="org", now=START + timedelta(hours=1),
    )
    return rebind_recovery_job_to_scope(store, **{**arguments, **overrides})


def test_running_global_worker_is_fenced(cli):
    cli.seed()
    with _store(cli.namespace) as worker, _store(cli.namespace) as operator:
        lease = worker.claim_writer("global-worker", expected_epoch=worker.writer_epoch())
        with FencedEnginePersistence(worker, lease).transaction():
            pass
        report = _rebind(operator)
        assert report.global_writer_epoch == lease.epoch + 1
        from sose.persistence.postgres import StaleWriterError

        with pytest.raises(StaleWriterError, match="stale writer epoch"):
            with FencedEnginePersistence(worker, lease).transaction():
                pass


def _admitted(store, trigger_id, *, correlation, organization):
    links = sql.Identifier(f"{store.namespace}_resource_intent_link")
    store._connection.execute(
        sql.SQL(
            "INSERT INTO {} (effect_id, organization_id, resource_key, causation_id, "
            "correlation_id, admitted_trigger_id) VALUES (%s, %s, 'k', 'c', %s, %s)"
        ).format(links),
        (f"effect-{uuid4().hex}", organization, correlation, trigger_id),
    )


@pytest.mark.parametrize(("correlation", "organization", "message"), [
    ("another-flow", "org", "does not own exactly one correlation"),
    ("flow", "another-org", "binds organization 'another-org'"),
])
def test_refuses_history_bound_to_another_correlation_or_organization(
    cli, correlation, organization, message,
):
    cli.seed()
    with _store(cli.namespace) as store:
        trigger = store.job_state(JOB).last_completed_trigger_id
        _admitted(store, trigger, correlation="flow", organization="org")
        _admitted(store, trigger, correlation=correlation, organization=organization)
        _admitted(store, "an-unrelated-job:slot", correlation="x", organization="y")
        epoch = store.writer_epoch()
        with pytest.raises(RecoveryRebindError, match=message):
            _rebind(store)
        assert store.writer_epoch() == epoch            # a healthy worker was not fenced
        assert store.job_state(JOB).config_json == "{}"


def test_history_of_the_same_binding_is_accepted(cli):
    cli.seed()
    with _store(cli.namespace) as store:
        trigger = store.job_state(JOB).last_completed_trigger_id
        _admitted(store, trigger, correlation="flow", organization="org")
        _admitted(store, "an-unrelated-job:slot", correlation="x", organization="y")
        assert _rebind(store).status == "rebound"


def test_namespace_without_resource_links_has_no_history_to_check():
    from sose.composition.recovery_rebind import _history_bindings
    from sose.jobs.model import SimulationJobState

    state = SimulationJobState(
        job_id=JOB, domain_name="trading_company_composition", config_json="{}",
        config_revision=1, status="ready", initialized=True, logical_time=START, next_tick=0,
    )
    with _store(f"rebind_{uuid4().hex[:12]}") as store:
        assert _history_bindings(store, state) == set()
