"""#637: job-scoped writer leases have one contract on SQLite and PostgreSQL.

Every scenario runs against SQLiteIncrementalPersistence and, when
SOSE_TEST_POSTGRES_DSN is set, PostgresPersistence. Messages, error types and
epoch arithmetic must match, because callers branch on them.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from sose.jobs.model import SimulationJobState
from sose.persistence.ownership import FencedEnginePersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
T0 = datetime(2026, 11, 18, tzinfo=timezone.utc)


def _sqlite(tmp_path):
    from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence, StaleWriterError

    return SQLiteIncrementalPersistence(tmp_path / "scoped.sqlite"), StaleWriterError


def _postgres(tmp_path):
    from sose.persistence.postgres import PostgresPersistence, StaleWriterError

    return PostgresPersistence(DSN, namespace=f"scw_{uuid4().hex[:12]}"), StaleWriterError


BACKENDS = [
    pytest.param(_sqlite, id="sqlite"),
    pytest.param(
        _postgres, id="postgres",
        marks=pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required"),
    ),
]


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path):
    store, stale = request.param(tmp_path)
    try:
        yield store, stale
    finally:
        store.close()


def _job(job_id="job-a", domain="domain", config="{}"):
    return SimulationJobState(
        job_id=job_id, domain_name=domain, config_json=config, config_revision=1,
        status="ready", initialized=False, logical_time=T0, next_tick=0,
    )


def test_scope_epochs_are_independent_of_each_other_and_of_the_global_writer(backend):
    store, _ = backend
    assert store.scoped_writer_epoch("a") == 0
    first = store.claim_scoped_writer("a", "w1", expected_epoch=0)
    second = store.claim_scoped_writer("a", "w2", expected_epoch=1)
    other = store.claim_scoped_writer("b", "w3", expected_epoch=0)
    assert (first.epoch, second.epoch, other.epoch) == (1, 2, 1)
    assert (second.owner_id, second.scope) == ("w2", "a")
    assert store.scoped_writer_epoch("a") == 2 and store.scoped_writer_epoch("b") == 1
    assert store.writer_epoch() == 0
    store.claim_writer("global", expected_epoch=0)
    assert store.scoped_writer_epoch("a") == 2


def test_lost_claim_and_stale_scoped_transaction_raise_the_same_messages(backend):
    store, stale = backend
    store.claim_scoped_writer("a", "w1", expected_epoch=0)
    with pytest.raises(stale, match=r"^scoped writer claim lost for a: expected 0, current 1$"):
        store.claim_scoped_writer("a", "w2", expected_epoch=0)
    with pytest.raises(stale, match=r"^stale scoped writer epoch 5 for a; current epoch is 1$"):
        with store.transaction(owner_epoch=5, writer_scope="a"):
            pass
    with pytest.raises(stale, match=r"^stale scoped writer epoch 1 for z; current epoch is -1$"):
        with store.transaction(owner_epoch=1, writer_scope="z"):
            pass


def test_current_scope_writes_even_while_the_global_writer_is_fenced(backend):
    store, _ = backend
    store.claim_writer("global", expected_epoch=0)        # global fencing now active
    lease = store.claim_scoped_writer("a", "w1", expected_epoch=0)
    with store.transaction(owner_epoch=lease.epoch, writer_scope="a") as uow:
        uow.save_job_state(_job())
    assert store.job_state("job-a") is not None


def test_fenced_view_carries_the_scope_into_transactions(backend):
    store, stale = backend
    lease = store.claim_scoped_writer("a", "w1", expected_epoch=0)
    fenced = FencedEnginePersistence(store, lease)
    with fenced.transaction() as uow:
        uow.save_job_state(_job())
    store.claim_scoped_writer("a", "w2", expected_epoch=1)      # w1 is now stale
    with pytest.raises(stale):
        with fenced.transaction() as uow:
            uow.save_job_state(_job(config='{"late": true}'))
    assert store.job_state("job-a").config_json == "{}"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"scope": "", "owner_id": "w"}, "scope and owner must be nonempty"),
        ({"scope": "a", "owner_id": ""}, "scope and owner must be nonempty"),
        ({"scope": "a", "owner_id": "w", "expected_epoch": -1}, "expected_epoch must be >= 0"),
        ({"scope": "a", "owner_id": "w", "correlation_id": ""}, "correlation_id must be nonempty"),
    ],
)
def test_claim_arguments_are_validated(backend, kwargs, message):
    store, _ = backend
    args = {"expected_epoch": 0, **kwargs}
    scope, owner = args.pop("scope"), args.pop("owner_id")
    with pytest.raises(ValueError, match=message):
        store.claim_scoped_writer(scope, owner, **args)


def test_scoped_transaction_requires_scope_and_epoch(backend):
    store, _ = backend
    with pytest.raises(ValueError, match="scoped transactions require scope and writer epoch"):
        with store.transaction(owner_epoch=None, writer_scope="a"):
            pass
    with pytest.raises(ValueError, match="scoped transactions require scope and writer epoch"):
        with store.transaction(owner_epoch=1, writer_scope=""):
            pass
    with pytest.raises(ValueError, match="writer scope must be nonempty"):
        store.scoped_writer_epoch("")


def test_recovery_correlation_binds_to_one_scope_for_life(backend):
    store, _ = backend
    store.claim_scoped_writer("a", "w1", expected_epoch=0, correlation_id="flow-1")
    store.claim_scoped_writer("a", "w1", expected_epoch=1, correlation_id="flow-1")
    with pytest.raises(ValueError, match="durable organizational binding changed"):
        store.claim_scoped_writer("a", "w1", expected_epoch=2, correlation_id="flow-2")
    with pytest.raises(ValueError, match="already owned by another job"):
        store.claim_scoped_writer("b", "w2", expected_epoch=0, correlation_id="flow-1")
    assert store.scoped_writer_epoch("a") == 2 and store.scoped_writer_epoch("b") == 0


def test_claim_and_register_commit_together_or_not_at_all(backend):
    store, _ = backend
    lease = store.claim_scoped_writer_and_register(
        "a", "w1", expected_epoch=0, correlation_id="flow-1", initial_job=_job(),
    )
    assert lease.epoch == 1 and store.job_state("job-a").domain_name == "domain"
    with pytest.raises(ValueError, match="already belongs to another domain"):
        store.claim_scoped_writer_and_register(
            "a", "w2", expected_epoch=1, correlation_id="flow-1",
            initial_job=_job(domain="other"),
        )
    assert store.scoped_writer_epoch("a") == 1          # rejected claim was rolled back
    again = store.claim_scoped_writer_and_register(
        "a", "w2", expected_epoch=1, correlation_id="flow-1", initial_job=_job(),
    )
    assert again.epoch == 2


def test_sqlite_database_without_scope_tables_gains_them_on_open(tmp_path):
    """Additive schema: a database created before #637 opens and supports scopes."""
    from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

    path = tmp_path / "old.sqlite"
    with SQLiteIncrementalPersistence(path) as store:
        store._connection.execute('DROP TABLE "sose_writer_scope"')
        store._connection.execute('DROP TABLE "sose_recovery_owner"')
        store._connection.commit()
    with SQLiteIncrementalPersistence(path) as store:
        assert store.claim_scoped_writer("a", "w1", expected_epoch=0).epoch == 1
