import sqlite3

from sose.persistence.authoritative_conformance import (
    AuthoritativePersistenceConformanceSuite,
    ConformanceCheckUnsupported,
)
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


class SQLiteIncrementalHarness:
    """Qualification probe for SQLite's declared single-writer envelope."""

    def __init__(self, path):
        self.path = path

    def _open(self):
        return SQLiteIncrementalPersistence(self.path)

    def atomic_uow(self):
        store = self._open()
        try:
            with store.transaction() as uow:
                uow.set_committed_tick(1)
            try:
                with store.transaction() as uow:
                    uow.set_committed_tick(2)
                    raise RuntimeError("fault injection")
            except RuntimeError:
                pass
            if store.committed_tick() != 1:
                raise AssertionError("rolled-back tick became visible")
        finally:
            store.close()

    def read_after_commit(self):
        writer = self._open()
        reader = self._open()
        try:
            with writer.transaction() as uow:
                uow.set_committed_tick(7)
            if reader.committed_tick() != 7:
                raise AssertionError("committed tick not visible to independent reader")
        finally:
            reader.close()
            writer.close()

    def conditional_ownership(self):
        raise ConformanceCheckUnsupported(
            "SQLite adapter has no SOSE conditional ownership primitive yet"
        )

    def stale_owner_fencing(self):
        raise ConformanceCheckUnsupported(
            "SQLite adapter has no SOSE fencing epoch yet"
        )

    def fresh_process_reconstruction(self):
        first = self._open()
        with first.transaction() as uow:
            uow.set_committed_tick(11)
        first.close()

        reopened = self._open()
        try:
            if reopened.committed_tick() != 11:
                raise AssertionError("reopened adapter did not reconstruct committed state")
        finally:
            reopened.close()

    def deterministic_continuation(self):
        first = self._open()
        with first.transaction() as uow:
            uow.set_committed_tick(20)
        first.close()

        resumed = self._open()
        with resumed.transaction() as uow:
            uow.set_committed_tick(resumed.committed_tick() + 1)
        resumed.close()

        final = self._open()
        try:
            if final.committed_tick() != 21:
                raise AssertionError("continuation diverged after reopen")
        finally:
            final.close()

    def terminal_identity_monotonicity(self):
        raise ConformanceCheckUnsupported(
            "terminal identity probe requires the shared identity-invariant fixture"
        )

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
            "SELECT schema_version, revision FROM sose_record_meta WHERE singleton = 1"
        ).fetchone()
        connection.close()
        if row != (2, 0):
            raise AssertionError(f"unexpected migrated metadata: {row!r}")


def test_sqlite_incremental_authority_baseline_is_explicit(tmp_path):
    result = AuthoritativePersistenceConformanceSuite(
        SQLiteIncrementalHarness(tmp_path / "authority.sqlite3")
    ).run()

    assert result.failed == ()
    assert result.passed == frozenset({
        "atomic_uow",
        "read_after_commit",
        "fresh_process_reconstruction",
        "deterministic_continuation",
        "schema_migration",
    })
    assert result.unsupported == frozenset({
        "conditional_ownership",
        "stale_owner_fencing",
        "terminal_identity_monotonicity",
    })
    assert result.authoritative is False
