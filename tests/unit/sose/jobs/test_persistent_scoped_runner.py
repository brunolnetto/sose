"""#637: PersistentJobRunner claims a lease scoped to its job (required since 0.10.0)."""
from __future__ import annotations

import warnings
from types import SimpleNamespace

import pytest

from sose.jobs.persistent import PersistentJobRunner
from sose.persistence.ownership import FencedEnginePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence, StaleWriterError


def _job(persistence, job_id="job-a"):
    return SimpleNamespace(
        job_id=job_id, definition="definition", persistence=persistence,
        backend_factory=None, ticks_per_trigger=1, max_ticks_per_trigger=1, sink_bindings=(),
    )


def test_jobs_in_one_namespace_get_independent_scoped_leases(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "jobs.sqlite") as store:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)   # scoped path never warns
            a = PersistentJobRunner(_job(store, "job-a"), owner_id="w1")._claim()
            b = PersistentJobRunner(_job(store, "job-b"), owner_id="w2")._claim()
        assert (a.scope, a.epoch, b.scope, b.epoch) == ("job-a", 1, "job-b", 1)
        assert store.writer_epoch() == 0          # the namespace-wide epoch is untouched
        # job-b's claim did not fence job-a out (the global writer used to).
        with FencedEnginePersistence(store, a).transaction():
            pass


def test_takeover_within_one_job_fences_the_previous_worker(tmp_path):
    with SQLiteIncrementalPersistence(tmp_path / "jobs.sqlite") as store:
        old = PersistentJobRunner(_job(store), owner_id="w1")._claim()
        new = PersistentJobRunner(_job(store), owner_id="w2")._claim()
        assert new.epoch == old.epoch + 1
        with pytest.raises(StaleWriterError, match="stale scoped writer epoch 1 for job-a"):
            with FencedEnginePersistence(store, old).transaction():
                pass


class _GlobalOnly:
    epoch = 0

    def writer_epoch(self):
        return self.epoch

    def claim_writer(self, owner_id, *, expected_epoch):
        self.epoch += 1
        return SimpleNamespace(owner_id=owner_id, epoch=self.epoch)


def test_global_only_persistence_is_refused():
    """The namespace-wide fallback deprecated in 0.9.0 was removed in 0.10.0."""
    with pytest.raises(TypeError, match="scoped_writer_epoch is missing"):
        PersistentJobRunner(_job(_GlobalOnly()), owner_id="w1")


def test_scoped_epoch_read_failure_propagates_without_retry():
    class _Broken(_GlobalOnly):
        calls = 0

        def scoped_writer_epoch(self, scope):
            _Broken.calls += 1
            raise RuntimeError("epoch read failed")

        def claim_scoped_writer(self, scope, owner_id, *, expected_epoch):
            raise AssertionError("never reached")

        def transaction(self, *, owner_epoch=None, writer_scope=None):
            raise AssertionError("never reached")

    runner = PersistentJobRunner(_job(_Broken()), owner_id="w1", claim_retries=3)
    with pytest.raises(RuntimeError, match="epoch read failed"):
        runner._claim()
    assert _Broken.calls == 1


def test_subclass_whose_transaction_lacks_writer_scope_is_refused(tmp_path):
    """A transaction override without writer_scope cannot carry a job-scoped lease."""
    from contextlib import contextmanager

    class _Legacy(SQLiteIncrementalPersistence):
        @contextmanager
        def transaction(self, *, owner_epoch=None, owner_id=None):
            with super().transaction(owner_epoch=owner_epoch, owner_id=owner_id) as uow:
                yield uow

    with _Legacy(tmp_path / "legacy.sqlite") as store:
        with pytest.raises(TypeError, match="writer_scope"):
            PersistentJobRunner(_job(store), owner_id="w1")
