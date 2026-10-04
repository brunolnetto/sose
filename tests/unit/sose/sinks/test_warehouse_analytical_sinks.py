from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from sose.core.events import DomainEvent
from sose.sinks.databricks import DatabricksAnalyticalSink
from sose.sinks.model import AnalyticalBatch
from sose.sinks.registry import builtin_sink_registry
from sose.sinks.snowflake import SnowflakeAnalyticalSink
from sose.sinks.sql_warehouse import validate_table_name


NOW = datetime(2026, 9, 28, 18, tzinfo=timezone.utc)


@dataclass
class FakeWarehouseState:
    batches: set[str]
    events: set[str]
    statements: list[tuple[str, object]]


class FakeCursor:
    def __init__(self, state: FakeWarehouseState) -> None:
        self.state = state
        self._fetchone = None
        self.closed = False

    def execute(self, statement, parameters=None):
        normalized = " ".join(str(statement).split())
        self.state.statements.append((normalized, parameters))
        upper = normalized.upper()

        if upper.startswith("SELECT 1 FROM"):
            batch_id = parameters[0]
            self._fetchone = (1,) if batch_id in self.state.batches else None
            return

        if upper.startswith("MERGE INTO"):
            values = tuple(parameters or ())
            if "SOSE_EVENTS" in upper or "EVENTS_TABLE" in upper:
                self.state.events.add(str(values[0]))
            elif "SOSE_BATCHES" in upper or "BATCHES_TABLE" in upper:
                self.state.batches.add(str(values[0]))

    def fetchone(self):
        return self._fetchone

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, state: FakeWarehouseState) -> None:
        self.state = state
        self.cursor_instance = FakeCursor(state)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _batch() -> AnalyticalBatch:
    events = (
        DomainEvent(
            event_id="event-1",
            name="created",
            entity_type="demo",
            entity_id="1",
            occurred_at=NOW,
            tick=1,
            payload={"value": 1},
        ),
        DomainEvent(
            event_id="event-2",
            name="updated",
            entity_type="demo",
            entity_id="1",
            occurred_at=NOW,
            tick=1,
            payload={"value": 2},
        ),
    )
    return AnalyticalBatch(
        batch_id="batch-1",
        job_id="job-1",
        domain_name="demo",
        config_revision=1,
        logical_tick=1,
        logical_time=NOW,
        from_event_offset=0,
        to_event_offset=2,
        events=events,
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda connection_factory: DatabricksAnalyticalSink(
            connection_factory,
            events_table="events_table",
            batches_table="batches_table",
        ),
        lambda connection_factory: SnowflakeAnalyticalSink(
            connection_factory,
            events_table="events_table",
            batches_table="batches_table",
        ),
    ],
)
def test_warehouse_sink_is_idempotent_by_batch_and_event(factory):
    state = FakeWarehouseState(set(), set(), [])
    connections = []

    def connect():
        connection = FakeConnection(state)
        connections.append(connection)
        return connection

    sink = factory(connect)
    batch = _batch()

    sink.publish(batch)
    sink.publish(batch)

    assert state.events == {"event-1", "event-2"}
    assert state.batches == {"batch-1"}
    assert len(connections) == 2
    assert all(connection.closed for connection in connections)

    merge_statements = [
        statement
        for statement, _ in state.statements
        if statement.upper().startswith("MERGE INTO")
    ]
    assert len(merge_statements) == 3
    assert "EVENTS_TABLE" in merge_statements[0].upper()
    assert "EVENTS_TABLE" in merge_statements[1].upper()
    assert "BATCHES_TABLE" in merge_statements[2].upper()


@pytest.mark.parametrize(
    "factory",
    [
        lambda connection_factory: DatabricksAnalyticalSink(
            connection_factory,
            events_table="events_table",
            batches_table="batches_table",
        ),
        lambda connection_factory: SnowflakeAnalyticalSink(
            connection_factory,
            events_table="events_table",
            batches_table="batches_table",
        ),
    ],
)
def test_partial_batch_retry_upserts_missing_events_before_marker(factory):
    state = FakeWarehouseState(set(), {"event-1"}, [])

    def connect():
        return FakeConnection(state)

    sink = factory(connect)
    sink.publish(_batch())

    assert state.events == {"event-1", "event-2"}
    assert state.batches == {"batch-1"}


@pytest.mark.parametrize(
    "sink_factory",
    [
        lambda connection_factory: DatabricksAnalyticalSink(
            connection_factory,
            events_table="events_table",
            batches_table="batches_table",
            create_tables=False,
        ),
        lambda connection_factory: SnowflakeAnalyticalSink(
            connection_factory,
            events_table="events_table",
            batches_table="batches_table",
            create_tables=False,
        ),
    ],
)
def test_warehouse_sink_skips_table_bootstrap_and_tolerates_noncallable_cleanup(
    sink_factory,
):
    state = FakeWarehouseState({"batch-1"}, set(), [])
    connection = FakeConnection(state)
    connection.commit = 123
    connection.close = 456
    connection.cursor_instance.close = 789

    sink = sink_factory(lambda: connection)
    sink.publish(_batch())

    statements = [statement.upper() for statement, _ in state.statements]
    assert statements and statements[0].startswith("SELECT 1 FROM")
    assert all("CREATE TABLE" not in statement for statement in statements)


@pytest.mark.parametrize(
    "sink_factory",
    [
        lambda connection_factory: DatabricksAnalyticalSink(
            connection_factory,
            events_table="events_table",
            batches_table="batches_table",
        ),
        lambda connection_factory: SnowflakeAnalyticalSink(
            connection_factory,
            events_table="events_table",
            batches_table="batches_table",
        ),
    ],
)
def test_warehouse_sink_tolerates_noncallable_commit_even_when_batch_is_new(
    sink_factory,
):
    state = FakeWarehouseState(set(), set(), [])
    connection = FakeConnection(state)
    connection.commit = 123

    sink = sink_factory(lambda: connection)
    sink.publish(_batch())

    assert state.events == {"event-1", "event-2"}
    assert state.batches == {"batch-1"}


def test_builtin_sink_registry_lists_warehouse_adapters_without_importing_extras():
    registry = builtin_sink_registry()
    assert registry.names() == ("databricks", "jsonl", "snowflake")
    assert registry.adapter("databricks").optional_extra == "databricks"
    assert registry.adapter("snowflake").optional_extra == "snowflake"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "events;drop",
        "catalog.bad-name.events",
        "1events",
        "catalog.schema.events table",
    ],
)
def test_warehouse_table_name_validation_rejects_unsafe_identifiers(value):
    with pytest.raises(ValueError, match="warehouse table"):
        validate_table_name(value)


@pytest.mark.parametrize(
    "value",
    [
        "events",
        "schema.events",
        "catalog.schema.events",
        "_events",
    ],
)
def test_warehouse_table_name_validation_accepts_safe_identifiers(value):
    assert validate_table_name(value) == value
