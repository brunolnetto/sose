"""Behavioral tests for batched command-existence refreshes."""

from datetime import datetime, timezone

import pytest

from sose.core.events import Command
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence


@pytest.mark.parametrize(
    ("adapter", "refresh_method"),
    (
        (SQLitePersistence, "_fresh"),
        (SQLiteIncrementalPersistence, "_fresh"),
        (DuckDBPersistence, "_fresh"),
        (JSONLJournalPersistence, "_refresh_from_journal"),
    ),
)
def test_batch_command_lookup_refreshes_once_and_observes_other_connection(
    tmp_path, adapter, refresh_method
):
    suffix = {
        SQLitePersistence: ".sqlite",
        SQLiteIncrementalPersistence: ".sqlite",
        DuckDBPersistence: ".duckdb",
        JSONLJournalPersistence: ".jsonl",
    }[adapter]
    path = tmp_path / f"{adapter.__name__}{suffix}"
    reader = adapter(path)
    writer = adapter(path)
    refresh_count = 0
    original_refresh = getattr(reader, refresh_method)

    def counted_refresh():
        nonlocal refresh_count
        refresh_count += 1
        return original_refresh()

    setattr(reader, refresh_method, counted_refresh)
    command = Command(
        command_id="command-written-by-peer",
        name="peer_write",
        entity_type="test",
        entity_id="entity-1",
        due_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        owner_id="test-owner",
    )

    try:
        # The reader's in-memory state predates this independent commit.
        with writer.transaction() as uow:
            uow.save_command(command)

        assert reader.missing_command_ids(
            (
                "command-written-by-peer",
                "missing-command",
                "missing-command",
            )
        ) == frozenset({"missing-command"})
        assert refresh_count == 1

        refresh_count = 0
        assert reader.missing_command_ids(()) == frozenset()
        assert refresh_count == 1
    finally:
        if hasattr(reader, "close"):
            reader.close()
        if hasattr(writer, "close"):
            writer.close()
