"""DuckLake bypasses the DuckDB constructor: inherited UoW must still track depth.

Mock only catalog bootstrapping so this falsifier runs without installing a
DuckLake extension/network; actual adapter persistence has separate tests.
"""
from datetime import datetime, timezone

import pytest

from sose.organizational.od3_decisions import QueuedWork
from sose.organizational.od3_queue_runtime import seed_od3_queue
from sose.persistence.ducklake import DuckLakePersistence


def test_ducklake_inherited_transaction_tracks_lifetime_without_local_db(
    monkeypatch, tmp_path,
):
    class FakeConnection:
        def __init__(self):
            self.statements = []

        def execute(self, statement, *args):
            self.statements.append(str(statement))
            return self

        def close(self):
            pass

    connection = FakeConnection()
    monkeypatch.setattr("sose.persistence.ducklake.duckdb.connect",
                        lambda _source: connection)
    monkeypatch.setattr(DuckLakePersistence, "_initialize_schema", lambda _self: None)
    monkeypatch.setattr(DuckLakePersistence, "_refresh_from_db", lambda _self: None)
    store = DuckLakePersistence(
        tmp_path / "catalog.duckdb",
        data_path=tmp_path / "data",
    )
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    items = (QueuedWork("item:1", "owner:1", now, 3),)
    assert store.transaction_depth == 0
    with store.transaction(owner_id="owner:1") as outer:
        assert store.transaction_depth == 1
        with pytest.raises(RuntimeError, match="nested transaction"):
            seed_od3_queue(
                store, owner_id="owner:1", queue_key="queue:1",
                items=items, at=now,
            )
        assert outer.events() == ()
    assert store.transaction_depth == 0
    assert "BEGIN TRANSACTION" in connection.statements
    assert "COMMIT" in connection.statements
    # The outer scope exited; a new transaction is once again admissible.
    queue_id = seed_od3_queue(
        store, owner_id="owner:1", queue_key="queue:1",
        items=items, at=now,
    )
    assert store.entity("od3_operational_queue", queue_id).state == "ready"
