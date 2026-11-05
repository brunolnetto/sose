from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from sose.core.events import DomainEvent
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="SOSE_TEST_POSTGRES_DSN is required for PostgreSQL integration tests",
)


def test_postgres_owner_event_count_refreshes_once_and_observes_peer_commit():
    assert DSN is not None
    namespace = f"event_count_{uuid4().hex[:16]}"
    reader = PostgresPersistence(DSN, namespace=namespace)
    writer = PostgresPersistence(DSN, namespace=namespace)

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    try:
        assert reader.count_events(owner_id="owner-a") == 0

        refresh_count = 0
        original_refresh = reader._refresh_from_db

        def counted_refresh(*, force: bool = False):
            nonlocal refresh_count
            refresh_count += 1
            return original_refresh(force=force)

        reader._refresh_from_db = counted_refresh

        with writer.transaction() as uow:
            for event_id, owner_id in (
                ("event-a", "owner-a"),
                ("event-b", "owner-b"),
            ):
                uow.append_event(
                    DomainEvent(
                        event_id=event_id,
                        name="test-event",
                        entity_type="test",
                        entity_id=event_id,
                        occurred_at=now,
                        owner_id=owner_id,
                    )
                )

        assert reader.count_events(owner_id="owner-a") == 1
        assert refresh_count == 1
    finally:
        reader.close()
        writer.close()
