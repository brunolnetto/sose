"""#637: MemoryPersistence writer fencing beyond the shared conformance suite.

The cross-backend contract lives in
tests/integration/sose/persistence/test_scoped_writer_conformance.py. These tests
cover what is specific to memory: commit-time re-validation, the namespace-wide
writer, read views, subclasses that replace ``transaction``, and the persistent
job runner that requires scoped writers.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.model import SimulationJobState
from sose.jobs.persistent import PersistentJobRunner
from sose.jobs.runner import SimulationJob
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.memory import (
    MemoryPersistence,
    ScopedWriterLease,
    StaleWriterError,
    WriterLease,
)
from sose.persistence.memory_fencing import FENCING_METHODS
from sose.persistence.ownership import FencedEnginePersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

T0 = datetime(2026, 11, 20, tzinfo=UTC)


def _job(job_id="job-a", domain="domain", config="{}"):
    return SimulationJobState(
        job_id=job_id, domain_name=domain, config_json=config, config_revision=1,
        status="ready", initialized=False, logical_time=T0, next_tick=0,
    )


def test_global_writer_claims_use_compare_and_swap():
    store = MemoryPersistence()
    assert store.writer_epoch() == 0
    lease = store.claim_writer("w1", expected_epoch=0)
    assert lease == WriterLease(owner_id="w1", epoch=1)
    with pytest.raises(StaleWriterError, match=r"^writer claim lost: expected epoch 0$"):
        store.claim_writer("w2", expected_epoch=0)
    with pytest.raises(ValueError, match="owner_id cannot be empty"):
        store.claim_writer("", expected_epoch=1)
    with pytest.raises(ValueError, match="expected_epoch must be >= 0"):
        store.claim_writer("w2", expected_epoch=-1)
    assert store.writer_epoch() == 1


def test_active_global_fencing_requires_the_current_epoch_for_unscoped_writes():
    store = MemoryPersistence()
    with store.transaction() as uow:                     # unfenced while the epoch is 0
        uow.save_job_state(_job("before"))
    store.claim_writer("w1", expected_epoch=0)
    with pytest.raises(StaleWriterError, match="fencing is active at epoch 1; owner_epoch is"):
        with store.transaction():
            pass
    with pytest.raises(StaleWriterError, match=r"^stale writer epoch 0; current epoch is 1$"):
        with store.transaction(owner_epoch=0):
            pass
    with store.transaction(owner_epoch=1) as uow:
        uow.save_job_state(_job("after"))
    assert {state.job_id for state in store.job_states()} == {"before", "after"}


def test_claim_between_fork_and_commit_fails_the_superseded_writer():
    """Memory claims never wait: the in-flight writer is fenced at commit instead."""
    store = MemoryPersistence()
    lease = store.claim_scoped_writer("a", "w1", expected_epoch=0)
    with pytest.raises(StaleWriterError, match="stale scoped writer epoch 1 for a"):
        with store.transaction(owner_epoch=lease.epoch, writer_scope="a") as uow:
            uow.save_job_state(_job())
            store.claim_scoped_writer("a", "w2", expected_epoch=1)
    assert store.job_state("job-a") is None


def test_explicit_commit_is_fenced_too():
    store = MemoryPersistence()
    lease = store.claim_writer("w1", expected_epoch=0)
    with pytest.raises(StaleWriterError, match="stale writer epoch 1; current epoch is 2"):
        with store.transaction(owner_epoch=lease.epoch) as uow:
            uow.save_job_state(_job())
            store.claim_writer("w2", expected_epoch=1)
            uow.commit()
    assert store.job_state("job-a") is None


def test_global_claim_and_register_is_atomic():
    store = MemoryPersistence()
    lease = store.claim_writer_and_register("w1", expected_epoch=0, initial_job=_job())
    assert lease.epoch == 1 and store.job_state("job-a").domain_name == "domain"
    with pytest.raises(ValueError, match="durable organizational binding changed"):
        store.claim_writer_and_register(
            "w2", expected_epoch=1, initial_job=_job(config='{"other": 1}'),
        )
    assert store.writer_epoch() == 1                     # rejected claim left no epoch
    with pytest.raises(StaleWriterError, match="writer claim lost"):
        store.claim_writer_and_register("w2", expected_epoch=0, initial_job=_job("job-b"))
    assert store.job_state("job-b") is None


def test_concurrent_claims_grant_each_epoch_once():
    store = MemoryPersistence()
    granted: list[ScopedWriterLease] = []
    barrier = threading.Barrier(8)

    def claim(index: int) -> None:
        barrier.wait()
        try:
            granted.append(store.claim_scoped_writer("a", f"w{index}", expected_epoch=0))
        except StaleWriterError:
            pass

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(granted) == 1 and store.scoped_writer_epoch("a") == 1


def test_read_views_refuse_claims_and_writes():
    store = MemoryPersistence()
    with store.read_snapshot() as view:
        with pytest.raises(RuntimeError, match="read snapshot is read-only"):
            view.claim_writer("w1", expected_epoch=0)
        with pytest.raises(RuntimeError, match="read snapshot is read-only"):
            view.claim_scoped_writer("a", "w1", expected_epoch=0)
    assert store.writer_epoch() == 0 and store.scoped_writer_epoch("a") == 0


def test_adapters_replacing_transaction_do_not_inherit_memory_fencing(tmp_path):
    """Their transactions never check a memory epoch, so they must not offer one."""
    for store in (
        JSONLJournalPersistence(tmp_path / "journal.jsonl"),
        SQLitePersistence(tmp_path / "full.sqlite"),
    ):
        for name in FENCING_METHODS:
            assert getattr(store, name) is None, (type(store).__name__, name)
    with SQLiteIncrementalPersistence(tmp_path / "inc.sqlite") as incremental:
        for name in FENCING_METHODS:
            assert callable(getattr(incremental, name))
        assert type(incremental).claim_scoped_writer is not MemoryPersistence.claim_scoped_writer


def test_subclasses_keeping_memory_transactions_keep_memory_fencing():
    class _Tagged(MemoryPersistence):
        tag = "kept"

    store = _Tagged()
    assert store.claim_scoped_writer("a", "w1", expected_epoch=0).epoch == 1
    with pytest.raises(StaleWriterError, match="stale scoped writer epoch 0 for a"):
        with store.transaction(owner_epoch=0, writer_scope="a"):
            pass


def test_persistent_job_runner_runs_on_memory_persistence():
    store = MemoryPersistence()
    job = SimulationJob(
        job_id="tutorial-persistent",
        definition=builtin_catalog().get("tutorial_job"),
        persistence=store,
        backend_factory=lambda origin: SimPyBackend(origin=origin),
    )
    job.initialize({"tick_step": timedelta(hours=1), "complete_after": timedelta(hours=2)})

    first = PersistentJobRunner(job, owner_id="w1").run_tick()
    assert (first.owner_id, first.epoch, first.result.logical_tick) == ("w1", 1, 1)
    second = PersistentJobRunner(job, owner_id="w2").run_tick()
    assert (second.owner_id, second.epoch, second.result.logical_tick) == ("w2", 2, 2)
    assert store.scoped_writer_epoch("tutorial-persistent") == 2
    assert store.writer_epoch() == 0                     # the namespace-wide epoch is untouched

    # w1's lease was superseded by w2's takeover.
    stale = FencedEnginePersistence(store, ScopedWriterLease("w1", 1, "tutorial-persistent"))
    with pytest.raises(StaleWriterError, match="stale scoped writer epoch 1"):
        with stale.transaction():
            pass
