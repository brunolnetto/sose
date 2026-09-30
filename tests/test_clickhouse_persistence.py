import sys
from types import SimpleNamespace

from sose.persistence.clickhouse import ClickHousePersistence


class FakeClient:
    def __init__(self):
        self.commands = []
        self.inserts = []
        self.closed = False

    def command(self, sql):
        self.commands.append(sql)

    def insert(self, table, rows, *, column_names):
        self.inserts.append((table, rows, column_names))

    def close(self):
        self.closed = True


def test_clickhouse_appends_committed_canonical_snapshot(monkeypatch):
    client = FakeClient()
    monkeypatch.setitem(
        sys.modules,
        "clickhouse_connect",
        SimpleNamespace(get_client=lambda **kwargs: client),
    )

    persistence = ClickHousePersistence(host="clickhouse")
    with persistence.transaction() as uow:
        uow.set_committed_tick(7)

    assert any("CREATE TABLE IF NOT EXISTS sose_record_snapshot" in sql for sql in client.commands)
    assert len(client.inserts) == 1
    table, rows, columns = client.inserts[0]
    assert table == "sose_record_snapshot"
    assert columns == [
        "snapshot_id",
        "collection",
        "record_key",
        "position",
        "payload",
    ]
    assert any(row[1] == "committed_tick" and row[4] == "7" for row in rows)

    persistence.close()
    assert client.closed is True
