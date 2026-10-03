from __future__ import annotations

import pytest

from sose.persistence.memory import _State


class _Cursor:
    def __init__(self, *, one=None, rowcount=1):
        self._one = one
        self.rowcount = rowcount

    def fetchone(self):
        return self._one


class _SequenceConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        if not self.responses:
            return _Cursor()
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def rollback(self):
        self.rollbacks += 1


def test_sqlite_refresh_rejects_newer_schema_codec_and_malformed_state(monkeypatch):
    import sose.persistence.sqlite as module

    persistence = object.__new__(module.SQLitePersistence)

    persistence._connection = _SequenceConnection(
        [_Cursor(one=(module.CURRENT_SCHEMA_VERSION + 1, module.CURRENT_CODEC_VERSION, "{}"))]
    )
    with pytest.raises(RuntimeError, match="schema was not migrated"):
        persistence._refresh_from_db()

    persistence._connection = _SequenceConnection(
        [_Cursor(one=(module.CURRENT_SCHEMA_VERSION, module.CURRENT_CODEC_VERSION + 1, "{}"))]
    )
    with pytest.raises(RuntimeError, match="codec is newer"):
        persistence._refresh_from_db()

    persistence._connection = _SequenceConnection(
        [_Cursor(one=(module.CURRENT_SCHEMA_VERSION, module.CURRENT_CODEC_VERSION, "{}"))]
    )
    monkeypatch.setattr(module, "loads", lambda payload: {"not": "state"})
    with pytest.raises(TypeError, match="does not contain SOSE durable state"):
        persistence._refresh_from_db()


def test_sqlite_transaction_rollback_restores_durable_state(tmp_path):
    from sose.persistence.sqlite import SQLitePersistence

    path = tmp_path / "rollback.sqlite3"
    persistence = SQLitePersistence(path)
    try:
        before = persistence.committed_tick()
        with pytest.raises(RuntimeError, match="boom"):
            with persistence.transaction() as uow:
                uow.set_committed_tick(before + 10)
                raise RuntimeError("boom")

        assert persistence.committed_tick() == before
    finally:
        persistence.close()


def test_incremental_refresh_short_circuits_when_revision_is_unchanged():
    from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

    persistence = object.__new__(SQLiteIncrementalPersistence)
    persistence._connection = _SequenceConnection([_Cursor(one=(7,))])
    persistence._revision = 7
    persistence._state = _State(committed_tick=4)

    persistence._refresh_from_db()

    assert persistence._state.committed_tick == 4
    assert len(persistence._connection.calls) == 1


def test_incremental_claim_writer_lost_compare_and_swap_rolls_back():
    from sose.persistence.sqlite_incremental import (
        SQLiteIncrementalPersistence,
        StaleWriterError,
    )

    persistence = object.__new__(SQLiteIncrementalPersistence)
    persistence._meta_table = '"sose_record_meta"'
    persistence._connection = _SequenceConnection([_Cursor(), _Cursor(rowcount=0)])

    with pytest.raises(StaleWriterError, match="writer claim lost"):
        persistence.claim_writer("worker", expected_epoch=3)

    assert persistence._connection.rollbacks == 1


def test_incremental_transaction_requires_current_writer_epoch(tmp_path):
    from sose.persistence.sqlite_incremental import (
        SQLiteIncrementalPersistence,
        StaleWriterError,
    )

    persistence = SQLiteIncrementalPersistence(tmp_path / "fenced.sqlite3")
    try:
        lease = persistence.claim_writer("worker-a", expected_epoch=0)

        with pytest.raises(StaleWriterError, match="owner_epoch is required"):
            with persistence.transaction():
                pass

        with pytest.raises(StaleWriterError, match="stale writer epoch"):
            with persistence.transaction(owner_epoch=lease.epoch + 1):
                pass

        with persistence.transaction(owner_epoch=lease.epoch):
            pass
    finally:
        persistence.close()


def test_incremental_persisted_record_count_handles_absent_row():
    from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

    persistence = object.__new__(SQLiteIncrementalPersistence)
    persistence._record_table = '"sose_record"'
    persistence._connection = _SequenceConnection([_Cursor(one=None)])

    assert persistence.persisted_record_count() == 0
