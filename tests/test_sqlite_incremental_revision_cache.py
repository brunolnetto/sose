import sqlite3

from sose.examples.tutorial_job.simulation import seed_job
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def test_incremental_sqlite_detects_external_commit_by_revision(tmp_path):
    path = tmp_path / "shared.sqlite3"
    first = SQLiteIncrementalPersistence(path)
    second = SQLiteIncrementalPersistence(path)

    job = seed_job(first, job_key="shared")

    restored = second.entity("tutorial_job", job.id)
    assert restored == job

    second.close()
    first.close()


def test_incremental_sqlite_revision_advances_only_on_change(tmp_path):
    path = tmp_path / "revision.sqlite3"
    persistence = SQLiteIncrementalPersistence(path)

    initial = persistence._database_revision()
    with persistence.transaction():
        pass
    assert persistence._database_revision() == initial

    seed_job(persistence, job_key="changed")
    assert persistence._database_revision() > initial
    persistence.close()


def test_incremental_sqlite_migrates_v1_meta_to_revision_schema(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE sose_record_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE sose_record (
            collection TEXT NOT NULL,
            record_key TEXT NOT NULL,
            position INTEGER NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (collection, record_key)
        )
        """
    )
    connection.execute(
        "INSERT INTO sose_record_meta(singleton, schema_version) VALUES (1, 1)"
    )
    connection.commit()
    connection.close()

    persistence = SQLiteIncrementalPersistence(path)

    row = persistence._connection.execute(
        "SELECT schema_version, revision FROM sose_record_meta WHERE singleton = 1"
    ).fetchone()
    assert row == (3, 0)
    persistence.close()


def test_incremental_sqlite_reuses_cached_state_when_revision_is_unchanged(tmp_path):
    path = tmp_path / "cache.sqlite3"
    persistence = SQLiteIncrementalPersistence(path)
    job = seed_job(persistence, job_key="cached")

    state_identity = id(persistence._state)
    assert persistence.entity("tutorial_job", job.id) == job
    assert id(persistence._state) == state_identity
    assert persistence.entity("tutorial_job", job.id) == job
    assert id(persistence._state) == state_identity

    persistence.close()
