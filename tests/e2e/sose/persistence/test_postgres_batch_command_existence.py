from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from sose.core.events import Command
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="SOSE_TEST_POSTGRES_DSN is required for PostgreSQL integration tests",
)


def test_batch_command_lookup_refreshes_once_and_observes_peer_commit():
    assert DSN is not None
    namespace = f"batch_{uuid4().hex[:16]}"
    reader = PostgresPersistence(DSN, namespace=namespace)
    writer = PostgresPersistence(DSN, namespace=namespace)

    try:
        # Prime the reader's cache before the independent writer commits.
        assert reader.command("command-written-by-peer") is None

        refresh_count = 0
        original_refresh = reader._refresh_from_db

        def counted_refresh(*, force: bool = False) -> None:
            nonlocal refresh_count
            refresh_count += 1
            original_refresh(force=force)

        reader._refresh_from_db = counted_refresh

        command = Command(
            command_id="command-written-by-peer",
            name="peer_write",
            entity_type="test",
            entity_id="entity-1",
            due_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            owner_id="test-owner",
        )
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
        reader.close()
        writer.close()
