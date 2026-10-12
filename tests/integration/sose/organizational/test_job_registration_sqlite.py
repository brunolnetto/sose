"""Cross-process job identity registration guard falsifiers for SQLite.

A recovery worker and a different domain creator cannot race across
preflight/claim. Neither a no-op guard nor SQLite's individual BEGIN IMMEDIATE
transactions suffice to protect several consecutive commits.
"""
from __future__ import annotations

from multiprocessing import get_context
import pytest

from sose.jobs.registration import job_registration_guard
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


_ADAPTERS = {
    "sqlite_snapshot": SQLitePersistence,
    "sqlite_incremental": SQLiteIncrementalPersistence,
}


def _enter_registration(path, adapter, job_id, ready, acquired):
    with _ADAPTERS[adapter](path) as store:
        ready.set()
        with job_registration_guard(store, job_id):
            acquired.set()


@pytest.mark.parametrize("adapter", tuple(_ADAPTERS))
def test_sqlite_registration_excludes_same_job_across_real_processes(tmp_path, adapter):
    path = str(tmp_path / "registration.db")
    ctx = get_context("spawn")
    ready, acquired = ctx.Event(), ctx.Event()
    with _ADAPTERS[adapter](path) as store:
        child = ctx.Process(
            target=_enter_registration,
            args=(path, adapter, "shared-job", ready, acquired),
        )
        try:
            with job_registration_guard(store, "shared-job"):
                child.start()
                assert ready.wait(timeout=20), "child never attempted registration"
                assert not acquired.wait(timeout=0.5), (
                    "different connection entered same job registration while locked"
                )
            child.join(timeout=20)
            if child.is_alive():
                pytest.fail("SQLite cross-process registration guard deadlocked")
            assert child.exitcode == 0
            assert acquired.is_set()
        finally:
            if child.is_alive():
                child.kill()
                child.join(timeout=15)


@pytest.mark.parametrize("adapter", tuple(_ADAPTERS))
def test_sqlite_independent_job_registration_is_not_globally_serialized(tmp_path, adapter):
    path = str(tmp_path / "independent.db")
    ctx = get_context("spawn")
    ready, acquired = ctx.Event(), ctx.Event()
    with _ADAPTERS[adapter](path) as store:
        with job_registration_guard(store, "org:A"):
            child = ctx.Process(
                target=_enter_registration,
                args=(path, adapter, "org:B", ready, acquired),
            )
            child.start()
            try:
                assert ready.wait(timeout=20)
                assert acquired.wait(timeout=5), "independent organization was serialized"
                child.join(timeout=15)
                assert not child.is_alive(), "independent registration worker did not finish"
                assert child.exitcode == 0
            finally:
                if child.is_alive():
                    child.kill()
                    child.join(timeout=15)


def _sqlite_recovery_claim_racer(path, reached_claim, release_claim, results):
    from datetime import datetime, timezone
    from unittest.mock import patch
    from sose.composition.recovery import TradingCustomerRecoveryRunner

    with SQLiteIncrementalPersistence(path) as store:
        real_claim = store.claim_writer_and_register

        def delayed_claim(*args, **kwargs):
            reached_claim.set()
            if not release_claim.wait(timeout=25):
                raise TimeoutError("test did not release pre-claim recovery worker")
            return real_claim(*args, **kwargs)

        store.claim_writer_and_register = delayed_claim
        runner = TradingCustomerRecoveryRunner(
            persistence=store, job_id="race:job", owner_id="recovery",
        )
        try:
            with patch.object(TradingCustomerRecoveryRunner, "_run_bounded", return_value=0):
                runner.run_trigger(
                    trigger_id="occurrence:1",
                    now=datetime(2026, 10, 12, tzinfo=timezone.utc),
                )
            results.put(("recovery", "accepted"))
        except (ValueError, RuntimeError) as error:
            results.put(("recovery", "rejected", str(error)))


def _sqlite_generic_registrar(path, ready, done, results):
    from sose.backends.simpy import SimPyBackend
    from sose.examples.catalog import builtin_catalog
    from sose.jobs.runner import SimulationJob

    with SQLiteIncrementalPersistence(path) as store:
        ready.set()
        job = SimulationJob(
            job_id="race:job", persistence=store,
            definition=builtin_catalog().get("tutorial_job"),
            backend_factory=lambda origin: SimPyBackend(origin=origin),
        )
        try:
            job.initialize()
            results.put(("generic", "accepted"))
        except (ValueError, RuntimeError) as error:
            results.put(("generic", "rejected", str(error)))
        finally:
            done.set()


def test_sqlite_recovery_claim_and_generic_registration_share_one_cross_process_fence(tmp_path):
    """A generic job cannot slip between PC6 domain preflight and writer claim."""
    path = str(tmp_path / "recovery-race.db")
    ctx = get_context("spawn")
    reached_claim, release_claim, generic_ready, generic_done = (ctx.Event() for _ in range(4))
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_sqlite_recovery_claim_racer,
            args=(path, reached_claim, release_claim, results),
        ),
        ctx.Process(
            target=_sqlite_generic_registrar,
            args=(path, generic_ready, generic_done, results),
        ),
    ]
    with SQLiteIncrementalPersistence(path) as store:
        initial_epoch = store.writer_epoch()
    try:
        processes[0].start()
        assert reached_claim.wait(timeout=25), "recovery never reached post-preflight claim"
        processes[1].start()
        assert generic_ready.wait(timeout=25)
        # A competing creator must remain excluded until the original
        # recovery commits its domain identity, not just its preflight read.
        assert not generic_done.wait(timeout=0.5), "foreign creator bypassed shared job registration guard"
        release_claim.set()
        for process in processes:
            process.join(timeout=35)
            if process.is_alive():
                process.kill()
                process.join()
                pytest.fail("registration racer deadlocked")
            assert process.exitcode == 0
        outcome = {record[0]: record[1] for record in (
            results.get(timeout=10), results.get(timeout=10),
        )}
        assert outcome == {"recovery": "accepted", "generic": "rejected"}
        with SQLiteIncrementalPersistence(path) as store:
            state = store.job_state("race:job")
            assert state.domain_name == "trading_company_composition"
            assert store.writer_epoch() == initial_epoch + 1
    finally:
        release_claim.set()
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join()
        results.close()
        results.join_thread()


def _die_after_global_writer_claim(path):
    import os
    from datetime import datetime, timezone
    from sose.composition.recovery import TradingCustomerRecoveryRunner

    with SQLiteIncrementalPersistence(path) as store:
        original_claim = store.claim_writer_and_register

        def committed_claim(*args, **kwargs):
            original_claim(*args, **kwargs)
            os._exit(87)

        store.claim_writer_and_register = committed_claim
        runner = TradingCustomerRecoveryRunner(
            persistence=store, job_id="crash:job", owner_id="worker:dead",
        )
        runner.run_trigger(
            trigger_id="stable-occurrence",
            now=datetime(2026, 10, 12, tzinfo=timezone.utc),
        )


def test_sqlite_worker_death_after_writer_claim_preserves_domain_identity(tmp_path):
    """A committed owner epoch can never exist without the owning job identity."""
    from datetime import datetime, timezone
    from unittest.mock import patch
    from sose.composition.recovery import TradingCustomerRecoveryRunner

    path = str(tmp_path / "writer-claim-death.db")
    ctx = get_context("spawn")
    child = ctx.Process(target=_die_after_global_writer_claim, args=(path,))
    child.start()
    child.join(timeout=30)
    if child.is_alive():
        child.kill()
        child.join()
        pytest.fail("writer-claim death worker hung")
    assert child.exitcode == 87

    with SQLiteIncrementalPersistence(path) as store:
        assert store.writer_epoch() == 1
        prior = store.job_state("crash:job")
        assert prior is not None
        assert prior.domain_name == "trading_company_composition"
        assert prior.initialized and prior.next_tick == 0
        runner = TradingCustomerRecoveryRunner(
            persistence=store, job_id="crash:job", owner_id="worker:recovered",
        )
        with patch.object(TradingCustomerRecoveryRunner, "_run_bounded", return_value=0):
            result = runner.run_trigger(
                trigger_id="stable-occurrence",
                now=datetime(2026, 10, 12, tzinfo=timezone.utc),
            )
        assert result.job_id == "crash:job"
        assert store.writer_epoch() == 2
        assert store.job_state("crash:job").last_completed_trigger_id == "stable-occurrence"
