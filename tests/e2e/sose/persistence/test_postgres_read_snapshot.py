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


def test_postgres_read_snapshot_uses_one_repeatable_read_observation():
    assert DSN is not None
    namespace = f"read_snap_{uuid4().hex[:16]}"
    reader = PostgresPersistence(DSN, namespace=namespace)
    writer = PostgresPersistence(DSN, namespace=namespace)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def append_event(event_id: str) -> None:
        with writer.transaction() as uow:
            uow.append_event(
                DomainEvent(
                    event_id=event_id,
                    name="read-snapshot-test",
                    entity_type="test",
                    entity_id=event_id,
                    occurred_at=now,
                    owner_id="owner-a",
                )
            )

    try:
        append_event("event-before-snapshot")
        original_revision = reader._database_revision
        injected = False

        def revision_then_peer_commit():
            nonlocal injected
            revision = original_revision()
            if not injected:
                injected = True
                append_event("event-after-snapshot-start")
            return revision

        reader._database_revision = revision_then_peer_commit

        with reader.read_snapshot() as snapshot:
            # The revision SELECT establishes the REPEATABLE READ view. The
            # peer's commit after that point must not leak into this snapshot.
            assert snapshot.count_events(owner_id="owner-a") == 1

        reader._database_revision = original_revision
        assert reader.count_events(owner_id="owner-a") == 2
    finally:
        reader.close()
        writer.close()
