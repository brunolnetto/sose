import sqlite3

import pytest

from sose.domain.entity import Entity
from sose.examples.tutorial_job.simulation import build_runtime, seed_job
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def _entity(entity_id: str, *, state: str = "ready", value: int = 0) -> Entity:
    return Entity(
        id=entity_id,
        entity_type="concurrency",
        state=state,
        attributes={"value": value},
        version=value,
    )


def test_independent_connections_observe_committed_writes(tmp_path):
    path = tmp_path / "shared.sqlite3"
    first = SQLiteIncrementalPersistence(path)
    second = SQLiteIncrementalPersistence(path)

    with first.transaction() as uow:
        uow.save_entity(_entity("one", value=1))

    assert second.entity("concurrency", "one") == _entity("one", value=1)

    first.close()
    second.close()


def test_alternating_writers_refresh_after_each_committed_revision(tmp_path):
    path = tmp_path / "alternating.sqlite3"
    first = SQLiteIncrementalPersistence(path)
    second = SQLiteIncrementalPersistence(path)

    with first.transaction() as uow:
        uow.save_entity(_entity("first", value=1))
    revision_after_first = first._database_revision()

    with second.transaction() as uow:
        assert uow.get_entity("concurrency", "first") == _entity(
            "first",
            value=1,
        )
        uow.save_entity(_entity("second", value=2))
    revision_after_second = second._database_revision()

    assert revision_after_second > revision_after_first
    assert first.entity("concurrency", "second") == _entity("second", value=2)

    first.close()
    second.close()


def test_cached_reader_detects_stale_revision_on_next_read(tmp_path):
    path = tmp_path / "stale-read.sqlite3"
    writer = SQLiteIncrementalPersistence(path)
    reader = SQLiteIncrementalPersistence(path)

    with writer.transaction() as uow:
        uow.save_entity(_entity("shared", value=1))

    assert reader.entity("concurrency", "shared") == _entity("shared", value=1)
    cached_revision = reader._revision

    with writer.transaction() as uow:
        current = uow.get_entity("concurrency", "shared")
        assert current is not None
        current.attributes["value"] = 2
        current.version = 2
        uow.save_entity(current)

    assert reader._revision == cached_revision
    assert reader.entity("concurrency", "shared") == _entity("shared", value=2)
    assert reader._revision > cached_revision

    writer.close()
    reader.close()


def test_second_writer_gets_explicit_lock_failure_while_first_holds_write_lock(
    tmp_path,
):
    path = tmp_path / "contention.sqlite3"
    first = SQLiteIncrementalPersistence(path)
    second = SQLiteIncrementalPersistence(path)
    second._connection.execute("PRAGMA busy_timeout = 50")

    first._connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with second.transaction() as uow:
                uow.save_entity(_entity("blocked", value=1))
    finally:
        first._connection.rollback()

    # Lock failure must not poison the losing adapter.
    with second.transaction() as uow:
        uow.save_entity(_entity("after-lock", value=2))
    assert first.entity("concurrency", "after-lock") == _entity(
        "after-lock",
        value=2,
    )

    first.close()
    second.close()


def test_conflicting_engine_commands_re_evaluate_latest_committed_state(tmp_path):
    path = tmp_path / "semantic-conflict.sqlite3"
    first = SQLiteIncrementalPersistence(path)
    job = seed_job(first, job_key="concurrent")
    second = SQLiteIncrementalPersistence(path)

    context_a, engine_a = build_runtime(first)
    context_b, engine_b = build_runtime(second)

    command_a = context_a.commands.create(
        "start",
        target=job,
        key=("concurrency", "start", "a"),
    )
    command_b = context_b.commands.create(
        "start",
        target=job,
        key=("concurrency", "start", "b"),
    )

    engine_a.dispatch(command_a)
    assert first.entity("tutorial_job", job.id).state == "running"

    revision_after_a = first._database_revision()
    engine_b.dispatch(command_b)

    persisted = second.entity("tutorial_job", job.id)
    assert persisted is not None
    assert persisted.state == "running"
    assert len(second.events()) == 1
    assert second._database_revision() == revision_after_a

    first.close()
    second.close()


def test_external_stale_object_is_last_writer_wins_outside_uow_read_discipline(
    tmp_path,
):
    path = tmp_path / "stale-object.sqlite3"
    first = SQLiteIncrementalPersistence(path)
    second = SQLiteIncrementalPersistence(path)

    with first.transaction() as uow:
        uow.save_entity(_entity("shared", value=1))

    stale = second.entity("concurrency", "shared")
    assert stale is not None

    with first.transaction() as uow:
        fresh = uow.get_entity("concurrency", "shared")
        assert fresh is not None
        fresh.attributes["value"] = 2
        fresh.version = 2
        uow.save_entity(fresh)

    stale.attributes["value"] = 3
    stale.version = 3
    with second.transaction() as uow:
        uow.save_entity(stale)

    # This is intentional current behavior: the persistence contract does not
    # attach compare-and-swap metadata to arbitrary detached Entity objects.
    assert first.entity("concurrency", "shared") == _entity("shared", value=3)

    first.close()
    second.close()
