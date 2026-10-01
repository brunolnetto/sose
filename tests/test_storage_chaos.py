from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat

import pytest

from sose.domain.entity import Entity
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


RUN = os.environ.get("SOSE_RUN_STORAGE_CHAOS") == "1"

pytestmark = pytest.mark.skipif(
    not RUN,
    reason="SOSE_RUN_STORAGE_CHAOS=1 is required",
)


def _large_entity(version: int = 1) -> Entity:
    return Entity(
        id="large-entity",
        entity_type="storage_chaos",
        state="ready",
        attributes={"payload": "x" * 2_000_000},
        version=version,
    )


def test_sqlite_disk_full_rolls_back_atomically_and_recovers(tmp_path):
    path = tmp_path / "disk-full.sqlite3"
    persistence = SQLiteIncrementalPersistence(path)

    with persistence.transaction() as uow:
        uow.set_committed_tick(7)
    baseline_count = persistence.persisted_record_count()
    baseline_revision = persistence._database_revision()

    page_count = int(
        persistence._connection.execute("PRAGMA page_count").fetchone()[0]
    )
    persistence._connection.execute(
        f"PRAGMA max_page_count = {page_count + 1}"
    )

    with pytest.raises(sqlite3.OperationalError, match="full"):
        with persistence.transaction() as uow:
            uow.save_entity(_large_entity())

    assert persistence.committed_tick() == 7
    assert persistence.entity("storage_chaos", "large-entity") is None
    assert persistence.persisted_record_count() == baseline_count
    assert persistence._database_revision() == baseline_revision

    persistence._connection.execute("PRAGMA max_page_count = 1000000")
    with persistence.transaction() as uow:
        uow.save_entity(_large_entity())

    assert persistence.entity("storage_chaos", "large-entity") == _large_entity()
    persistence.close()

    reopened = SQLiteIncrementalPersistence(path)
    assert reopened.committed_tick() == 7
    assert reopened.entity("storage_chaos", "large-entity") == _large_entity()
    reopened.close()


@pytest.mark.skipif(os.name != "posix", reason="filesystem permission semantics require POSIX")
def test_sqlite_read_only_filesystem_preserves_state_and_recovers(tmp_path):
    directory = tmp_path / "readonly"
    directory.mkdir()
    path = directory / "engine.sqlite3"

    first = SQLiteIncrementalPersistence(path)
    with first.transaction() as uow:
        uow.set_committed_tick(11)
        uow.save_entity(
            Entity(
                id="entity-1",
                entity_type="storage_chaos",
                state="ready",
                attributes={"value": "before"},
                version=1,
            )
        )
    first.close()

    for suffix in ("-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        candidate.unlink(missing_ok=True)

    original_dir_mode = stat.S_IMODE(directory.stat().st_mode)
    original_file_mode = stat.S_IMODE(path.stat().st_mode)
    os.chmod(path, 0o444)
    os.chmod(directory, 0o555)
    try:
        with pytest.raises((sqlite3.OperationalError, PermissionError)):
            blocked = SQLiteIncrementalPersistence(path)
            try:
                with blocked.transaction() as uow:
                    uow.set_committed_tick(12)
            finally:
                blocked.close()
    finally:
        os.chmod(directory, original_dir_mode)
        os.chmod(path, original_file_mode)

    recovered = SQLiteIncrementalPersistence(path)
    assert recovered.committed_tick() == 11
    entity = recovered.entity("storage_chaos", "entity-1")
    assert entity is not None
    assert entity.attributes["value"] == "before"

    with recovered.transaction() as uow:
        current = uow.get_entity("storage_chaos", "entity-1")
        assert current is not None
        current.attributes["value"] = "after"
        current.version = 2
        uow.save_entity(current)
        uow.set_committed_tick(12)
    recovered.close()

    final = SQLiteIncrementalPersistence(path)
    assert final.committed_tick() == 12
    entity = final.entity("storage_chaos", "entity-1")
    assert entity is not None
    assert entity.attributes["value"] == "after"
    assert entity.version == 2
    final.close()


def test_live_sqlite_write_denial_rolls_back_and_can_retry(tmp_path):
    path = tmp_path / "write-denied.sqlite3"
    persistence = SQLiteIncrementalPersistence(path)
    with persistence.transaction() as uow:
        uow.set_committed_tick(20)

    persistence._connection.execute("PRAGMA query_only = ON")
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            with persistence.transaction() as uow:
                uow.set_committed_tick(21)
    finally:
        persistence._connection.execute("PRAGMA query_only = OFF")

    assert persistence.committed_tick() == 20
    with persistence.transaction() as uow:
        uow.set_committed_tick(21)
    assert persistence.committed_tick() == 21
    persistence.close()

    reopened = SQLiteIncrementalPersistence(path)
    assert reopened.committed_tick() == 21
    reopened.close()
