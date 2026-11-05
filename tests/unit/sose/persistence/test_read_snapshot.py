from datetime import datetime, timezone

import pytest

from sose.core.events import DomainEvent
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

ADAPTERS = (
    pytest.param(SQLitePersistence, ".sqlite", id="sqlite-snapshot"),
    pytest.param(
        SQLiteIncrementalPersistence,
        ".sqlite",
        id="sqlite-incremental",
    ),
    pytest.param(DuckDBPersistence, ".duckdb", id="duckdb"),
    pytest.param(JSONLJournalPersistence, ".jsonl", id="jsonl-journal"),
)


def _append_event(store, event_id: str) -> None:
    with store.transaction() as uow:
        uow.append_event(
            DomainEvent(
                event_id=event_id,
                name="read-snapshot-test",
                entity_type="test",
                entity_id=event_id,
                occurred_at=NOW,
                owner_id="owner-a",
            )
        )


def test_memory_read_snapshot_rejects_opening_inside_write_transaction():
    store = MemoryPersistence()

    with store.transaction():
        with pytest.raises(RuntimeError, match="inside a write transaction"):
            with store.read_snapshot():
                pass


def test_memory_read_snapshot_is_detached_and_read_only():
    store = MemoryPersistence()
    _append_event(store, "event-a")

    with store.read_snapshot() as snapshot:
        assert snapshot.count_events(owner_id="owner-a") == 1
        with pytest.raises(RuntimeError, match="read snapshot is read-only"):
            with snapshot.transaction():
                pass
        _append_event(store, "event-b")
        assert snapshot.count_events(owner_id="owner-a") == 1

    assert store.count_events(owner_id="owner-a") == 2


@pytest.mark.parametrize(("adapter", "suffix"), ADAPTERS)
def test_persistent_read_snapshot_loads_once_and_survives_peer_commit(
    tmp_path,
    adapter,
    suffix,
):
    path = tmp_path / f"read-snapshot{suffix}"
    reader = adapter(path)
    writer = adapter(path)
    try:
        _append_event(writer, "event-a")

        load_count = 0
        original_snapshot_state = reader._snapshot_state

        def counted_snapshot_state():
            nonlocal load_count
            load_count += 1
            return original_snapshot_state()

        reader._snapshot_state = counted_snapshot_state

        with reader.read_snapshot() as snapshot:
            with reader.read_snapshot() as nested:
                assert nested is snapshot
            assert snapshot.count_events(owner_id="owner-a") == 1
            _append_event(writer, "event-b")
            assert snapshot.count_events(owner_id="owner-a") == 1
            assert load_count == 1

        # The next ordinary read still refreshes and observes the peer commit.
        assert reader.count_events(owner_id="owner-a") == 2

        with pytest.raises(RuntimeError, match="snapshot scope failure"):
            with reader.read_snapshot():
                raise RuntimeError("snapshot scope failure")
        assert reader.count_events(owner_id="owner-a") == 2
    finally:
        for store in (reader, writer):
            close = getattr(store, "close", None)
            if close is not None:
                close()


def test_jsonl_read_snapshot_uses_captured_file_boundary(
    tmp_path,
    monkeypatch,
):
    import os

    import sose.persistence.jsonl_journal as jsonl_module

    path = tmp_path / "snapshot-boundary.jsonl"
    reader = JSONLJournalPersistence(path)
    writer = JSONLJournalPersistence(path)

    try:
        _append_event(writer, "event-a")

        real_fstat = os.fstat
        append_after_capture = True

        def fstat_with_concurrent_append(fd):
            nonlocal append_after_capture

            result = real_fstat(fd)
            if append_after_capture and os.path.samefile(
                f"/proc/self/fd/{fd}",
                path,
            ):
                append_after_capture = False
                _append_event(writer, "event-b")

            return result

        monkeypatch.setattr(jsonl_module.os, "fstat", fstat_with_concurrent_append)

        with reader.read_snapshot() as snapshot:
            assert snapshot.count_events(owner_id="owner-a") == 1

        assert reader.count_events(owner_id="owner-a") == 2
    finally:
        for store in (reader, writer):
            close = getattr(store, "close", None)
            if close is not None:
                close()
