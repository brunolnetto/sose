from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from sose.testing.restart import restart_reference_runtime


@pytest.mark.parametrize(
    "factory,filename",
    [
        (SQLiteIncrementalPersistence, "state.sqlite3"),
        (JSONLJournalPersistence, "state.jsonl"),
    ],
)
def test_sink_survives_close_reopen_and_backend_rebuild(tmp_path, factory, filename):
    path = tmp_path / filename
    first = factory(path)
    job = seed_job(first)
    _, engine = build_runtime(first)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    complete_at = ORIGIN + timedelta(hours=2)

    start_and_schedule_completion(
        first,
        engine,
        job_id=job.id,
        complete_at=complete_at,
    )
    if hasattr(first, "close"):
        first.close()

    reopened = factory(path)
    rebuilt = restart_reference_runtime(
        reopened,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(complete_at)

    restored = reopened.entity("tutorial_job", job.id)
    assert restored is not None and restored.state == "completed"
    assert reopened.scheduled_work() == ()
    if hasattr(reopened, "close"):
        reopened.close()


def test_incremental_sqlite_writes_records_not_whole_state(tmp_path):
    persistence = SQLiteIncrementalPersistence(tmp_path / "records.sqlite3")
    job = seed_job(persistence)

    assert persistence.persisted_record_count() > 0
    columns = {
        row[1]
        for row in persistence._connection.execute(
            "PRAGMA table_info(sose_record)"
        ).fetchall()
    }
    assert columns == {"collection", "record_key", "position", "payload"}
    assert persistence.entity("tutorial_job", job.id) == job
    persistence.close()


def test_jsonl_journal_replays_only_complete_transaction_lines(tmp_path):
    path = tmp_path / "journal.jsonl"
    persistence = JSONLJournalPersistence(path)
    job = seed_job(persistence)
    assert persistence.journal_transaction_count() >= 1

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version":1,"transaction":999,"changes":[')

    reopened = JSONLJournalPersistence(path)
    assert reopened.entity("tutorial_job", job.id) == job
