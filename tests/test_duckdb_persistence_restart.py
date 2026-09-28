from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.duckdb import DuckDBPersistence
from sose.testing.restart import restart_reference_runtime


def test_duckdb_survives_close_reopen_and_backend_rebuild(tmp_path):
    path = tmp_path / "state.duckdb"
    first = DuckDBPersistence(path)
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
    first.close()

    reopened = DuckDBPersistence(path)
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
    reopened.close()


def test_duckdb_persists_backend_neutral_records(tmp_path):
    persistence = DuckDBPersistence(tmp_path / "records.duckdb")
    job = seed_job(persistence)

    assert persistence.persisted_record_count() > 0
    row = persistence._connection.execute(
        "SELECT payload FROM sose_record "
        "WHERE collection = 'entities' LIMIT 1"
    ).fetchone()
    assert row is not None
    assert persistence.entity("tutorial_job", job.id) == job
    persistence.close()
