"""Cross-process job identity registration guard falsifiers for SQLite.

A recovery worker and a different domain creator cannot race across
preflight/claim. Neither a no-op guard nor SQLite's individual BEGIN IMMEDIATE
transactions suffice to protect several consecutive commits.
"""
from __future__ import annotations

from multiprocessing import get_context
from queue import Empty

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
        with job_registration_guard(store, "shared-job"):
            child = ctx.Process(
                target=_enter_registration,
                args=(path, adapter, "shared-job", ready, acquired),
            )
            child.start()
            try:
                assert ready.wait(timeout=20), "child never attempted registration"
                assert not acquired.wait(timeout=0.5), (
                    "different connection entered same job registration while locked"
                )
            finally:
                pass
        child.join(timeout=20)
        if child.is_alive():
            child.kill()
            child.join()
            pytest.fail("SQLite cross-process registration guard deadlocked")
        assert child.exitcode == 0
        assert acquired.is_set()


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
            finally:
                if child.is_alive():
                    child.kill()
                child.join(timeout=15)
            assert child.exitcode == 0
