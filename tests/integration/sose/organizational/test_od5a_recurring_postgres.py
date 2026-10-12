"""Real PostgreSQL scoped-lease falsifiers for bounded #485 source-step bridge.

Same job: two independent PostgreSQL connections must NOT both get an epoch-1
lease. Different jobs must retain independent clocks. Native SimPy resource
arbitration and the boundary reconciler remain subsequent gates.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.core.events import DomainEvent
from dataclasses import replace
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_recurring import (
    initialize_od5a_recurring_job,
    claim_od5a_recurring_tick, execute_od5a_claimed_tick,
    StaleOD5aRunnerClaimError,
)
from sose.organizational.od5a_durable import od5a_durable_events
from sose.persistence.postgres import PostgresPersistence
from sose.persistence.memory import MemoryUnitOfWork


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real SOSE_TEST_POSTGRES_DSN required")
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
LEASE = timedelta(seconds=10)


def _namespace():
    return "od5arun_" + uuid4().hex[:16]


def _arm():
    spec = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.5, "execution_slots": 1,
            "coordination_latency": 0.1, "service_cv": 0.5,
        },
        parameter_evidence={
            key: EvidenceClass.ASSUMED for key in
            ("arrival_rate", "execution_slots", "coordination_latency", "service_cv")
        },
    )
    return compile_od5a_arms(
        baseline=spec, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            k: OD5aChargedCost(8, 1, 1, 1)
            for k in ("capacity_only", "topology_only",
                      "coordination_only", "structural_combined")
        },
    )[0]


def _init(namespace):
    with PostgresPersistence(DSN, namespace=namespace) as store:
        initialize_od5a_recurring_job(
            store, job_id="org:race", arm=_arm(),
            items=(OD5aWorkItem(
                "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
            ),),
        )


def _claim_worker(dsn, namespace, owner, barrier, reports):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        # Connections must be opened BEFORE the contested SQL transaction.
        barrier.wait(timeout=30)
        try:
            result = claim_od5a_recurring_tick(
                store, job_id="org:race", owner_id=owner,
                now=NOW, lease_duration=LEASE,
                trigger_id=deterministic_id("od5a-recurring-tick", "org:race", 1),
            )
            reports.put((owner, "claimed", result.epoch))
        except StaleOD5aRunnerClaimError:
            reports.put((owner, "fenced", None))


def test_distinct_postgres_connections_cannot_claim_same_step_epoch():
    namespace = _namespace()
    _init(namespace)
    ctx = get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [
        ctx.Process(target=_claim_worker, args=(DSN, namespace, name, barrier, results))
        for name in ("worker:A", "worker:B")
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=45)
            if process.is_alive():
                process.kill()
                process.join()
                pytest.fail("PostgreSQL runner claim hung")
            assert process.exitcode == 0
        outcome = [results.get(timeout=10) for _ in processes]
        assert sorted(state for _, state, _ in outcome) == ["claimed", "fenced"]
        assert sorted(epoch for _, state, epoch in outcome if state == "claimed") == [1]
        with PostgresPersistence(DSN, namespace=namespace) as store:
            state = store.job_state("org:race")
            assert state.next_tick == 0 and state.status == "running"
            with store.transaction() as uow:
                lease = uow.get_entity("od5a_runner_lease", "org:race")
                winner = next(owner for owner, status, _ in outcome if status == "claimed")
                assert lease.attributes["owner_id"] == winner
                assert lease.attributes["epoch"] == 1
            assert not od5a_durable_events(store, job_id="org:race")
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join()
        results.close()
        results.join_thread()


def _kill_after_pg_claim(dsn, namespace):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        claim_od5a_recurring_tick(
            store, job_id="org:race", owner_id="worker:dead",
            now=NOW, lease_duration=timedelta(seconds=3),
            trigger_id=deterministic_id("od5a-recurring-tick", "org:race", 1),
        )
        os._exit(91)


def test_recovery_of_postgres_job_after_real_claimed_worker_death():
    namespace = _namespace()
    _init(namespace)
    ctx = get_context("spawn")
    child = ctx.Process(target=_kill_after_pg_claim, args=(DSN, namespace))
    child.start()
    child.join(timeout=40)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("PostgreSQL runner failed to die after claimed step")
    assert child.exitcode == 91
    trigger = deterministic_id("od5a-recurring-tick", "org:race", 1)
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with pytest.raises(StaleOD5aRunnerClaimError):
            claim_od5a_recurring_tick(
                store, job_id="org:race", owner_id="worker:recovered",
                now=NOW + timedelta(seconds=1), lease_duration=LEASE,
                trigger_id=trigger, recover=True,
            )
        # Wait for REAL PostgreSQL clock time to cross the OS-dead worker's
        # committed lease deadline; do not forge a future client timestamp.
        with store.transaction() as uow:
            old_lease = uow.get_entity("od5a_runner_lease", "org:race")
            expiry = datetime.fromisoformat(old_lease.attributes["expires_at"])
        remaining = (expiry - store.trusted_utc_now()).total_seconds()
        if remaining > 0:
            sleep(remaining + 0.1)
        recovered = claim_od5a_recurring_tick(
            store, job_id="org:race", owner_id="worker:recovered",
            now=NOW + timedelta(seconds=11), lease_duration=LEASE,
            trigger_id=trigger, recover=True,
        )
        assert recovered.epoch == 2
        step = execute_od5a_claimed_tick(
            store, claim=recovered, now=NOW + timedelta(seconds=12),
        )
        assert step.action == "advanced" and step.logical_tick == 1
        assert len(od5a_durable_events(store, job_id="org:race")) == 1
        assert store.job_state("org:race").last_completed_trigger_id == trigger


def _standalone_source_seed(namespace):
    from sose.organizational.od5a_durable import initialize_od5a_durable
    with PostgresPersistence(DSN, namespace=namespace) as store:
        initialize_od5a_durable(
            store, job_id="org:race", arm=_arm(),
            items=(OD5aWorkItem(
                "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
            ),),
        )


def _ownership_racer(dsn, namespace, kind, barrier, results):
    from sose.organizational.od5a_durable import advance_od5a_durable
    with PostgresPersistence(dsn, namespace=namespace) as store:
        barrier.wait(timeout=30)
        try:
            if kind == "binding":
                initialize_od5a_recurring_job(
                    store, job_id="org:race", arm=_arm(),
                    items=(OD5aWorkItem(
                        "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
                    ),),
                )
            else:
                advance_od5a_durable(store, job_id="org:race")
            results.put((kind, "committed"))
        except (ValueError, RuntimeError) as exc:
            results.put((kind, "rejected"))


def test_postgres_source_advance_cannot_race_recurring_ownership_transfer():
    """Either the standalone step or the job bind commits, never a split pair."""
    namespace = _namespace()
    _standalone_source_seed(namespace)
    ctx = get_context("spawn")
    barrier, reports = ctx.Barrier(2), ctx.Queue()
    procs = [
        ctx.Process(
            target=_ownership_racer,
            args=(DSN, namespace, kind, barrier, reports),
        )
        for kind in ("binding", "advance")
    ]
    try:
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=45)
            if proc.is_alive():
                proc.kill()
                proc.join()
                pytest.fail("PostgreSQL ownership-transfer racer hung")
            assert proc.exitcode == 0
        outcomes = dict(reports.get(timeout=10) for _ in procs)
        assert sorted(outcomes.values()) == ["committed", "rejected"]
        with PostgresPersistence(DSN, namespace=namespace) as store:
            state = store.job_state("org:race")
            with store.transaction() as uow:
                source = uow.get_entity("od5a_durable_pilot", "org:race")
            if outcomes["binding"] == "committed":
                assert state is not None and source.attributes["step"] == state.next_tick == 0
            else:
                assert state is None and source.attributes["step"] == 1
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.kill()
                proc.join()
        reports.close()
        reports.join_thread()



def _domain_registration_worker(dsn, namespace, domain, started, finished, results):
    from sose.backends.simpy import SimPyBackend
    from sose.examples.catalog import builtin_catalog
    from sose.jobs.runner import SimulationJob

    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            started.set()
            if domain == "od5a":
                initialize_od5a_recurring_job(
                    store, job_id="org:race", arm=_arm(),
                    items=(OD5aWorkItem(
                        "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
                    ),),
                )
            elif domain == "composition":
                from unittest.mock import patch
                from sose.composition.recovery import TradingCustomerRecoveryRunner
                runner = TradingCustomerRecoveryRunner(
                    persistence=store, owner_id="worker:composition",
                    job_id="org:race", correlation_id="org:race",
                    scoped_writer=True,
                )
                # Isolate its real registration/fencing transaction from the
                # unrelated customer boundary/drain business effect.
                with patch.object(TradingCustomerRecoveryRunner, "_run_bounded", return_value=0):
                    runner.run_trigger(trigger_id="registration", now=NOW)
            else:
                job = SimulationJob(
                    job_id="org:race",
                    definition=builtin_catalog().get("tutorial_job"),
                    persistence=store,
                    backend_factory=lambda origin: SimPyBackend(origin=origin),
                )
                job.initialize()
        results.put((domain, "accepted"))
    except ValueError:
        results.put((domain, "rejected"))
    finally:
        finished.set()


@pytest.mark.parametrize("domain", ["od5a", "tutorial", "composition"])
def test_postgres_job_registration_respects_shared_identity_guard(domain):
    """Every job-state creator must acquire the SAME job identity guard."""
    namespace = _namespace()
    ctx = get_context("spawn")
    started, finished, reports = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(
        target=_domain_registration_worker,
        args=(DSN, namespace, domain, started, finished, reports),
    )
    try:
        with PostgresPersistence(DSN, namespace=namespace) as store:
            with store.business_resource_guard("sose:job-registration:org:race"):
                process.start()
                assert started.wait(timeout=30), "worker did not reach initialization"
                sleep(0.25)
                assert not finished.is_set(), (
                    f"{domain} initialization ignored shared job registration guard"
                )
        process.join(timeout=40)
        if process.is_alive():
            process.kill()
            process.join()
            pytest.fail("registration worker hung after guard release")
        assert process.exitcode == 0
        assert reports.get(timeout=5) == (domain, "accepted")
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        reports.close()
        reports.join_thread()



def _competing_domain_registrar(dsn, namespace, domain, barrier, results):
    from sose.backends.simpy import SimPyBackend
    from sose.examples.catalog import builtin_catalog
    from sose.jobs.runner import SimulationJob

    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            barrier.wait(timeout=30)
            if domain == "od5a":
                initialize_od5a_recurring_job(
                    store, job_id="org:race", arm=_arm(),
                    items=(OD5aWorkItem(
                        "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
                    ),),
                )
            elif domain == "composition":
                from unittest.mock import patch
                from sose.composition.recovery import TradingCustomerRecoveryRunner
                runner = TradingCustomerRecoveryRunner(
                    persistence=store, owner_id="worker:composition",
                    job_id="org:race", correlation_id="org:race",
                    scoped_writer=True,
                )
                # Isolate its real registration/fencing transaction from the
                # unrelated customer boundary/drain business effect.
                with patch.object(TradingCustomerRecoveryRunner, "_run_bounded", return_value=0):
                    runner.run_trigger(trigger_id="registration", now=NOW)
            else:
                job = SimulationJob(
                    job_id="org:race",
                    definition=builtin_catalog().get("tutorial_job"),
                    persistence=store,
                    backend_factory=lambda origin: SimPyBackend(origin=origin),
                )
                job.initialize()
        results.put((domain, "accepted"))
    except ValueError:
        results.put((domain, "rejected"))


@pytest.mark.parametrize("opponent", ["tutorial", "composition"])
def test_postgres_cross_domain_registration_has_one_authoritative_owner(opponent):
    namespace = _namespace()
    ctx = get_context("spawn")
    barrier, reports = ctx.Barrier(2), ctx.Queue()
    processes = [
        ctx.Process(
            target=_competing_domain_registrar,
            args=(DSN, namespace, domain, barrier, reports),
        )
        for domain in ("od5a", opponent)
    ]
    try:
        for proc in processes:
            proc.start()
        for proc in processes:
            proc.join(timeout=45)
            if proc.is_alive():
                proc.kill()
                proc.join()
                pytest.fail("cross-domain job registration deadlocked")
            assert proc.exitcode == 0
        outcomes = dict(reports.get(timeout=10) for _ in processes)
        assert sorted(outcomes.values()) == ["accepted", "rejected"]
        winner = next(domain for domain, outcome in outcomes.items()
                      if outcome == "accepted")
        with PostgresPersistence(DSN, namespace=namespace) as store:
            state = store.job_state("org:race")
            assert state is not None and state.initialized
            assert state.domain_name == (
                "od5a" if winner == "od5a" else (
                    "trading_company_composition" if winner == "composition"
                    else "tutorial_job"
                )
            )
            with store.transaction() as uow:
                pilot = uow.get_entity("od5a_durable_pilot", "org:race")
                if winner == "od5a":
                    assert pilot is not None
                    assert pilot.attributes["step"] == state.next_tick == 0
                else:
                    assert pilot is None
    finally:
        for proc in processes:
            if proc.is_alive():
                proc.kill()
                proc.join()
        reports.close()
        reports.join_thread()



def test_composition_recovery_rejects_foreign_job_with_matching_config():
    from unittest.mock import patch
    from sose.composition.recovery import TradingCustomerRecoveryRunner
    from sose.jobs.model import SimulationJobState

    namespace = _namespace()
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.transaction() as uow:
            uow.save_job_state(SimulationJobState(
                job_id="org:race", domain_name="foreign-domain",
                config_json='{"correlation_id":"org:race"}',
                config_revision=1, status="ready", initialized=True,
                logical_time=NOW, next_tick=0,
            ))
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker:composition",
            job_id="org:race", correlation_id="org:race",
            scoped_writer=True,
        )
        writer_epoch_before = store.writer_epoch()
        scoped_epoch_before = store.scoped_writer_epoch("org:race")
        with patch.object(TradingCustomerRecoveryRunner, "_run_bounded", return_value=0):
            with pytest.raises(ValueError, match="already belongs to another domain"):
                runner.run_trigger(trigger_id="registration", now=NOW)
        assert store.job_state("org:race").domain_name == "foreign-domain"
        assert store.writer_epoch() == writer_epoch_before
        assert store.scoped_writer_epoch("org:race") == scoped_epoch_before
        # Rejected registration cannot poison the unique correlation binding.
        lease = store.claim_scoped_writer(
            "legitimate-recovery", "owner:legitimate", expected_epoch=0,
            correlation_id="org:race",
        )
        assert lease.epoch == 1


@pytest.mark.parametrize("scoped", [True, False])
def test_foreign_job_never_consumes_writer_epoch_before_validation(scoped):
    from sose.composition.recovery import TradingCustomerRecoveryRunner
    from sose.jobs.model import SimulationJobState

    namespace = _namespace()
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.transaction() as uow:
            uow.save_job_state(SimulationJobState(
                job_id="foreign-job", domain_name="foreign-domain",
                config_json="{}", config_revision=1, status="ready",
                initialized=True, logical_time=NOW, next_tick=0,
            ))
        old_global_epoch = store.writer_epoch()
        old_scoped_epoch = store.scoped_writer_epoch("foreign-job")
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="intruder", job_id="foreign-job",
            correlation_id="tenant:foreign" if scoped else None,
            scoped_writer=scoped,
        )
        with pytest.raises(ValueError, match="already belongs to another domain"):
            runner.run_trigger(trigger_id="attempt", now=NOW)
        assert store.writer_epoch() == old_global_epoch
        assert store.scoped_writer_epoch("foreign-job") == old_scoped_epoch



def _die_after_postgres_atomic_recovery_registration(dsn, namespace, scoped):
    from sose.composition.recovery import TradingCustomerRecoveryRunner

    with PostgresPersistence(dsn, namespace=namespace) as store:
        method_name = (
            "claim_scoped_writer_and_register" if scoped
            else "claim_writer_and_register"
        )
        original = getattr(store, method_name)

        def committed_claim(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(96)

        setattr(store, method_name, committed_claim)
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker:dead", job_id="org:crash",
            correlation_id="org:crash" if scoped else None,
            scoped_writer=scoped,
        )
        runner.run_trigger(trigger_id="stable-trigger", now=NOW)


@pytest.mark.parametrize("scoped", [False, True])
def test_postgres_death_after_atomic_writer_claim_keeps_recoverable_domain_identity(scoped):
    from unittest.mock import patch
    from sose.composition.recovery import TradingCustomerRecoveryRunner

    namespace = _namespace()
    ctx = get_context("spawn")
    child = ctx.Process(
        target=_die_after_postgres_atomic_recovery_registration,
        args=(DSN, namespace, scoped),
    )
    child.start()
    child.join(timeout=45)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("PostgreSQL recovery claim-death child hung")
    assert child.exitcode == 96
    with PostgresPersistence(DSN, namespace=namespace) as store:
        state = store.job_state("org:crash")
        assert state is not None
        assert state.domain_name == "trading_company_composition"
        assert state.initialized and state.next_tick == 0
        assert (
            store.scoped_writer_epoch("org:crash") if scoped else store.writer_epoch()
        ) == 1
        runner = TradingCustomerRecoveryRunner(
            persistence=store, owner_id="worker:recovered", job_id="org:crash",
            correlation_id="org:crash" if scoped else None,
            scoped_writer=scoped,
        )
        with patch.object(TradingCustomerRecoveryRunner, "_run_bounded", return_value=0):
            result = runner.run_trigger(trigger_id="stable-trigger", now=NOW)
        assert result.job_id == "org:crash"
        assert store.job_state("org:crash").last_completed_trigger_id == "stable-trigger"
        assert (
            store.scoped_writer_epoch("org:crash") if scoped else store.writer_epoch()
        ) == 2


def test_server_clock_controls_pg_lease_even_when_worker_clock_is_far_ahead():
    """No host-clock skew can prematurely grant a second PostgreSQL epoch."""
    namespace = _namespace()
    _init(namespace)
    trigger = deterministic_id("od5a-recurring-tick", "org:race", 1)
    fake_future = datetime.now(timezone.utc) + timedelta(days=365)
    with PostgresPersistence(DSN, namespace=namespace) as store:
        server_before = store.trusted_utc_now()
        original = claim_od5a_recurring_tick(
            store, job_id="org:race", owner_id="worker:A",
            now=fake_future, lease_duration=LEASE, trigger_id=trigger,
        )
        server_after = store.trusted_utc_now()
        assert server_before + LEASE <= original.lease_expires_at
        assert original.lease_expires_at <= server_after + LEASE
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with pytest.raises(StaleOD5aRunnerClaimError):
            claim_od5a_recurring_tick(
                store, job_id="org:race", owner_id="worker:B",
                now=fake_future + timedelta(days=1), lease_duration=LEASE,
                trigger_id=trigger, recover=True,
            )
        with pytest.raises(ValueError, match="PostgreSQL trusted lease time"):
            claim_od5a_recurring_tick(
                store, job_id="org:race", owner_id="worker:B",
                now=fake_future, lease_duration=LEASE, trigger_id=trigger,
                recover=True, _wall_clock=lambda: fake_future,
            )
        with store.transaction() as uow:
            lease = uow.get_entity("od5a_runner_lease", "org:race")
            assert lease.attributes["owner_id"] == "worker:A"
            assert lease.attributes["epoch"] == 1



def test_two_organizations_append_events_without_overwriting_each_other(
    monkeypatch,
):
    """Two committed checkpoints imply two distinct durable event identities.

    Force both workers to append after reading the same global event count.
    Locking only each job cannot defend index-keyed PostgreSQL event records.
    """
    namespace = _namespace()
    items = (OD5aWorkItem(
        "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
    ),)
    with PostgresPersistence(DSN, namespace=namespace) as store:
        for organization in ("org:A", "org:B"):
            initialize_od5a_recurring_job(
                store, job_id=organization, arm=_arm(), items=items,
            )
        claims = {
            organization: claim_od5a_recurring_tick(
                store, job_id=organization, owner_id="owner:" + organization,
                now=NOW, lease_duration=timedelta(seconds=45),
                trigger_id=deterministic_id(
                    "od5a-recurring-tick", organization, 1,
                ),
            )
            for organization in ("org:A", "org:B")
        }
        assert store.events() == ()

    barrier = Barrier(2)
    original = MemoryUnitOfWork.append_event

    def synchronize_append(uow, event):
        original(uow, event)
        if event.name.startswith("od5a."):
            barrier.wait(timeout=25)

    monkeypatch.setattr(MemoryUnitOfWork, "append_event", synchronize_append)

    def execute(organization):
        with PostgresPersistence(DSN, namespace=namespace) as worker:
            return execute_od5a_claimed_tick(
                worker, claim=claims[organization], now=NOW,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(execute, organization)
            for organization in ("org:A", "org:B")
        ]
        steps = [future.result(timeout=40) for future in futures]
    assert [step.action for step in steps] == ["advanced", "advanced"]

    with PostgresPersistence(DSN, namespace=namespace) as restored:
        events = restored.events()
        assert len(events) == 2
        assert len({event.event_id for event in events}) == 2
        assert {event.entity_id for event in events} == {"org:A", "org:B"}
        for organization in ("org:A", "org:B"):
            state = restored.job_state(organization)
            assert state.run_count == 1 and state.next_tick == 1
            assert state.active_trigger_id is None
            org_events = od5a_durable_events(restored, job_id=organization)
            assert len(org_events) == 1



def test_postgres_repeated_identical_domain_event_is_idempotent_but_conflicts_fail():
    """Do not mistake exact replay for a conflicting event identity."""
    namespace = _namespace()
    event = DomainEvent(
        event_id="fixed-causal-event", name="audit.test",
        entity_type="shipment", entity_id="shipment:A",
        occurred_at=NOW, payload={"stage": "received"},
    )
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.transaction() as uow:
            uow.append_event(event)
        with store.transaction() as uow:
            uow.append_event(event)
        assert store.events() == (event,)
        with pytest.raises(ValueError, match="conflicting PostgreSQL durable event ID"):
            with store.transaction() as uow:
                uow.append_event(replace(event, payload={"stage": "tampered"}))
        assert store.events() == (event,)



def test_postgres_legacy_duplicate_event_identity_conflicts_fail_closed():
    """Every old numeric-key row must agree before replay is considered safe."""
    from psycopg import sql
    from sose.persistence.codec import dumps

    namespace = _namespace()
    event = DomainEvent(
        event_id="shared-legacy-event", name="audit.test",
        entity_type="shipment", entity_id="shipment:A", occurred_at=NOW,
        payload={"stage": "received"},
    )
    conflicting = replace(event, payload={"stage": "dispatched"})
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store._connection.transaction():
            for index, value in enumerate((event, conflicting)):
                store._connection.execute(
                    sql.SQL(
                        "INSERT INTO {}(collection, record_key, position, payload) "
                        "VALUES (%s, %s, %s, %s)"
                    ).format(store._record_table),
                    ("events", str(index), index, dumps(value)),
                )
            store._increment_revision()

    with PostgresPersistence(DSN, namespace=namespace) as store:
        assert len(store.events()) == 2
        with pytest.raises(ValueError, match="conflicting PostgreSQL durable event ID"):
            with store.transaction() as uow:
                uow.append_event(event)
        assert store.events() == (event, conflicting)


def test_postgres_concurrent_equal_events_with_distinct_codec_bytes_are_idempotent(
    monkeypatch,
):
    """An identity race compares decoded event truth, not JSON byte order."""
    from datetime import timezone, timedelta
    namespace = _namespace()
    event = DomainEvent(
        event_id="concurrent-semantic-event", name="audit.test",
        entity_type="shipment", entity_id="shipment:A", occurred_at=NOW,
        payload={"first": 1, "second": 2},
    )
    equivalent = replace(
        event,
        occurred_at=NOW.astimezone(timezone(timedelta(hours=-3))),
        payload={"second": 2, "first": 1},
    )
    assert equivalent == event
    from sose.persistence.codec import dumps
    assert dumps(equivalent) != dumps(event)

    barrier = Barrier(2)
    original = MemoryUnitOfWork.append_event

    def concurrent_append(uow, append):
        original(uow, append)
        if append.event_id == event.event_id:
            barrier.wait(timeout=25)

    monkeypatch.setattr(MemoryUnitOfWork, "append_event", concurrent_append)

    def writer(value):
        with PostgresPersistence(DSN, namespace=namespace) as store:
            with store.transaction() as uow:
                uow.append_event(value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, value) for value in (event, equivalent)]
        for future in futures:
            future.result(timeout=40)

    with PostgresPersistence(DSN, namespace=namespace) as store:
        assert len(store.events()) == 1
        assert store.events()[0] == event
