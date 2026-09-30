import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from sose.core.events import Command
from sose.core.runtime import (
    DurableStoreItem,
    ScheduledWork,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
)
from sose.persistence.authoritative_conformance import (
    AuthoritativePersistenceConformanceSuite,
)
from sose.persistence.qualification import ConcurrencyEnvelope
from sose.persistence.sqlite_incremental import (
    SQLiteIncrementalPersistence,
    StaleWriterError,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SQLiteIncrementalHarness:
    """Qualification probe for SQLite's declared single-writer envelope."""

    concurrency = ConcurrencyEnvelope(max_writers=1, distributed=False)

    def __init__(self, path):
        self.path = path

    def _open(self):
        return SQLiteIncrementalPersistence(self.path)

    def atomic_uow(self):
        store = self._open()
        try:
            with store.transaction() as uow:
                uow.set_committed_tick(1)
            with pytest.raises(RuntimeError, match="fault injection"):
                with store.transaction() as uow:
                    uow.set_committed_tick(2)
                    raise RuntimeError("fault injection")
            assert store.committed_tick() == 1
        finally:
            store.close()

    def read_after_commit(self):
        writer = self._open()
        reader = self._open()
        try:
            with writer.transaction() as uow:
                uow.set_committed_tick(7)
            assert reader.committed_tick() == 7
        finally:
            reader.close()
            writer.close()

    def conditional_ownership(self):
        first = self._open()
        second = self._open()
        try:
            lease = first.claim_writer("worker-a", expected_epoch=0)
            assert lease.epoch == 1
            with pytest.raises(StaleWriterError):
                second.claim_writer("worker-b", expected_epoch=0)
        finally:
            second.close()
            first.close()

    def stale_owner_fencing(self):
        first = self._open()
        second = self._open()
        try:
            current = sqlite3.connect(self.path).execute(
                "SELECT owner_epoch FROM sose_record_meta WHERE singleton = 1"
            ).fetchone()
            expected = int(current[0])
            lease_a = first.claim_writer("worker-a", expected_epoch=expected)
            lease_b = second.claim_writer("worker-b", expected_epoch=lease_a.epoch)
            assert lease_b.epoch == lease_a.epoch + 1
            with pytest.raises(StaleWriterError):
                with first.transaction(owner_epoch=lease_a.epoch) as uow:
                    uow.set_committed_tick(99)
            assert second.committed_tick() != 99
        finally:
            second.close()
            first.close()

    def fresh_process_reconstruction(self):
        first = self._open()
        with first.transaction() as uow:
            uow.set_committed_tick(11)
        first.close()

        code = (
            "from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence;"
            f"p=SQLiteIncrementalPersistence({str(self.path)!r});"
            "assert p.committed_tick()==11;"
            "p.close()"
        )
        subprocess.run([sys.executable, "-c", code], check=True)

    def _seed_representative_state(self, store):
        command = Command(
            command_id="cmd-1",
            name="continue",
            entity_type="test",
            entity_id="entity-1",
            due_at=NOW,
        )
        work = ScheduledWork(
            work_id="work-1",
            due_at=NOW,
            priority=3,
            sequence=4,
            command_id=command.command_id,
        )
        with store.transaction() as uow:
            uow.save_command(command)
            uow.save_scheduled_work(work)
            uow.set_committed_tick(20)

    @staticmethod
    def _projection(store):
        command = store.command("cmd-1")
        return (
            store.committed_tick(),
            command,
            store.scheduled_work(),
        )

    def deterministic_continuation(self):
        reference_path = self.path.with_name("reference.sqlite3")
        restarted_path = self.path.with_name("restarted.sqlite3")

        reference = SQLiteIncrementalPersistence(reference_path)
        self._seed_representative_state(reference)
        with reference.transaction() as uow:
            uow.set_committed_tick(21)
        expected = self._projection(reference)
        reference.close()

        first = SQLiteIncrementalPersistence(restarted_path)
        self._seed_representative_state(first)
        first.close()
        resumed = SQLiteIncrementalPersistence(restarted_path)
        with resumed.transaction() as uow:
            uow.set_committed_tick(21)
        actual = self._projection(resumed)
        resumed.close()

        assert actual == expected

    def terminal_identity_monotonicity(self):
        path = self.path.with_name("identity.sqlite3")
        store = SQLiteIncrementalPersistence(path)
        definition = StoreDefinition(name="parts")
        request = StoreGetRequest(
            request_id="get-1",
            store_name="parts",
            requested_at=NOW,
            sequence=1,
        )
        item = DurableStoreItem(
            item_id="item-1",
            store_name="parts",
            value={"sku": "A"},
            priority=0,
            sequence=1,
        )
        result = StoreGetResult(
            request_id=request.request_id,
            store_name="parts",
            item=item,
            completed_at=NOW,
            sequence=2,
        )
        try:
            with store.transaction() as uow:
                uow.save_store_definition(definition)
                uow.save_store_get_request(request)

            with pytest.raises(ValueError, match="pending and completed"):
                with store.transaction() as uow:
                    uow.save_store_get_result(result)

            with store.transaction() as uow:
                uow.save_store_get_result(result)
                uow.delete_store_get_request(request.request_id)

            reopened = SQLiteIncrementalPersistence(path)
            try:
                assert reopened.store_get_results() == (result,)
                with pytest.raises(ValueError, match="pending and completed"):
                    with reopened.transaction() as uow:
                        uow.save_store_get_request(request)
            finally:
                reopened.close()
        finally:
            store.close()

    def schema_migration(self):
        migration_path = self.path.with_name("migration.sqlite3")
        connection = sqlite3.connect(migration_path)
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
        connection.execute("INSERT INTO sose_record_meta VALUES (1, 1)")
        connection.commit()
        connection.close()

        migrated = SQLiteIncrementalPersistence(migration_path)
        migrated.close()

        connection = sqlite3.connect(migration_path)
        row = connection.execute(
            """
            SELECT schema_version, revision, owner_id, owner_epoch
            FROM sose_record_meta WHERE singleton = 1
            """
        ).fetchone()
        connection.close()
        assert row == (3, 0, None, 0)


def test_sqlite_incremental_earns_authoritative_single_writer_qualification(tmp_path):
    harness = SQLiteIncrementalHarness(tmp_path / "authority.sqlite3")
    suite = AuthoritativePersistenceConformanceSuite(harness)

    result = suite.run()
    qualification = suite.qualify(result)

    assert result.failed == ()
    assert result.unsupported == frozenset()
    assert result.authoritative is True
    assert qualification.authoritative is True
    assert qualification.concurrency == ConcurrencyEnvelope(
        max_writers=1,
        distributed=False,
    )
