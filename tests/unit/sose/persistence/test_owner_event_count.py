from datetime import datetime, timezone

import pytest

from sose.core.events import DomainEvent
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from sose.persistence.duckdb import DuckDBPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

ADAPTERS = (
    pytest.param(SQLitePersistence, "_fresh", ".sqlite", id="sqlite-snapshot"),
    pytest.param(
        SQLiteIncrementalPersistence,
        "_fresh",
        ".sqlite",
        id="sqlite-incremental",
    ),
    pytest.param(DuckDBPersistence, "_fresh", ".duckdb", id="duckdb"),
    pytest.param(
        JSONLJournalPersistence,
        "_refresh_from_journal",
        ".jsonl",
        id="jsonl-journal",
    ),
)


def _event(event_id: str, owner_id: str, payload=None) -> DomainEvent:
    return DomainEvent(
        event_id=event_id,
        name="test-event",
        entity_type="test",
        entity_id=event_id,
        occurred_at=NOW,
        payload={} if payload is None else payload,
        owner_id=owner_id,
    )


def test_memory_owner_event_count_is_scoped_and_events_remain_defensive():
    store = MemoryPersistence()

    with store.transaction() as uow:
        uow.append_event(
            _event(
                "event-a",
                "owner-a",
                {"nested": {"values": [1]}},
            )
        )
        uow.append_event(_event("event-b", "owner-b"))

    assert store.count_events(owner_id="owner-a") == 1
    assert store.count_events(owner_id="owner-b") == 1
    assert store.count_events(owner_id="owner-c") == 0

    # The optimization must not weaken the existing events() isolation.
    returned = store.events()
    returned[0].payload["nested"]["values"].append(2)

    persisted = next(
        event for event in store.events()
        if event.event_id == "event-a"
    )
    assert persisted.payload["nested"]["values"] == [1]


@pytest.mark.parametrize(
    ("adapter", "refresh_method", "suffix"),
    ADAPTERS,
)
def test_owner_event_count_refreshes_once_and_observes_peer_commit(
    tmp_path,
    adapter,
    refresh_method,
    suffix,
):
    path = tmp_path / f"{adapter.__name__}{suffix}"
    reader = adapter(path)
    writer = adapter(path)

    try:
        # Prime the reader before another connection commits.
        assert reader.count_events(owner_id="owner-a") == 0

        refresh_count = 0
        original_refresh = getattr(reader, refresh_method)

        def counted_refresh():
            nonlocal refresh_count
            refresh_count += 1
            return original_refresh()

        setattr(reader, refresh_method, counted_refresh)

        with writer.transaction() as uow:
            uow.append_event(_event("event-a", "owner-a"))
            uow.append_event(_event("event-b", "owner-b"))

        assert reader.count_events(owner_id="owner-a") == 1
        assert refresh_count == 1

        refresh_count = 0
        assert reader.count_events(owner_id="owner-b") == 1
        assert refresh_count == 1
    finally:
        for persistence in (reader, writer):
            close = getattr(persistence, "close", None)
            if close is not None:
                close()


def test_memory_owner_event_count_rejects_blank_owner():
    store = MemoryPersistence()

    for owner_id in ("", "  ", None):
        with pytest.raises(ValueError, match="owner_id"):
            store.count_events(owner_id=owner_id)
