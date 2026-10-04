from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sose.domain.warehouse import DomainApplyResult
from sose.jobs.model import CompletedJobTrigger, SimulationJobState
from sose.persistence.codec import _decode, _encode, _resolve, dumps, loads
from sose.persistence.memory import _State
from sose.persistence.records import (
    StateRecord,
    changes_for_dirty_records,
    diff_state_records,
    records_to_state,
    state_to_records,
)
from sose.persistence.registry import (
    PersistenceAdapter,
    PersistenceCapabilities,
    PersistenceRegistry,
    _resolve_path as resolve_persistence_path,
    builtin_persistence_registry,
)
from sose.sinks.registry import (
    SinkAdapter,
    SinkRegistry,
    _option_or_env,
    _resolve_path as resolve_sink_path,
    builtin_sink_registry,
)


class SampleEnum(Enum):
    FIRST = "first"


def test_codec_round_trips_all_supported_semantic_types(tmp_path):
    identifier = uuid4()
    record = StateRecord("collection", "key", 3, "payload")
    value = {
        "none": None,
        "bool": True,
        "int": 7,
        "float": 1.25,
        "datetime": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "date": date(2026, 1, 2),
        "uuid": identifier,
        "bytes": b"\x00\xff",
        "path": tmp_path / "state.json",
        "enum": DomainApplyResult.APPLIED,
        "custom_enum": SampleEnum.FIRST,
        "dataclass": record,
        "tuple": (1, "two"),
        "list": [1, 2],
        "frozenset": frozenset({"b", "a"}),
        "set": {3, 1},
        "dict": {(1, 2): "value"},
    }

    restored = loads(dumps(value))

    assert restored == value
    assert dumps({"set": {"b", "a"}}) == dumps({"set": {"a", "b"}})


def test_codec_resolve_and_decode_reject_invalid_payloads():
    with pytest.raises(ValueError, match="invalid SOSE class path"):
        _resolve("missing-colon")
    with pytest.raises(ValueError, match="local classes cannot be restored"):
        _resolve("sose.persistence.records:<locals>.Thing")
    with pytest.raises(TypeError, match="is not a type"):
        _resolve("sose.persistence.records:dumps")
    with pytest.raises(TypeError, match="unsupported SOSE persistence value"):
        _encode(object())
    with pytest.raises(TypeError, match="invalid tagged SOSE value"):
        _decode(1.5)
    with pytest.raises(ValueError, match="unknown SOSE persistence tag"):
        loads(json.dumps({"__sose_type__": "unknown"}))

    assert _decode([1, {"plain": 2}]) == [1, {"plain": 2}]
    assert _decode({"plain": 2}) == {"plain": 2}


def test_state_records_round_trip_dict_list_and_scalar_shapes():
    state = _State(
        commands={"b": 2, "a": 1},
        events=[1, 2],
        committed_tick=3,
    )

    records = state_to_records(state)
    restored = records_to_state(records.values())

    assert restored.commands == {"b": 2, "a": 1}
    assert restored.events == [1, 2]
    assert restored.committed_tick == 3

    # Missing scalar records fall back to the _State default.
    without_tick = [
        record
        for record in records.values()
        if record.collection != "committed_tick"
    ]
    assert records_to_state(without_tick).committed_tick == -1


def test_diff_state_records_emits_deterministic_delete_and_upserts():
    before = _State(commands={"same": 1, "changed": 2, "deleted": 3})
    after = _State(commands={"same": 1, "changed": 20, "added": 4})

    changes = diff_state_records(before, after)

    operations = [(change.operation, loads(change.key)) for change in changes if change.collection == "commands"]
    assert operations == [
        ("delete", "deleted"),
        ("upsert", "added"),
        ("upsert", "changed"),
    ]
    assert diff_state_records(after, after) == ()


def test_dirty_record_changes_cover_dict_lifecycle():
    before = _State(commands={"same": 1, "changed": 2, "deleted": 3})
    after = _State(commands={"same": 1, "changed": 20, "added": 4})

    changes = changes_for_dirty_records(
        before,
        after,
        {
            ("commands", "same"),
            ("commands", "changed"),
            ("commands", "deleted"),
            ("commands", "added"),
            ("commands", "absent"),
        },
    )

    by_key = {
        loads(change.key): change
        for change in changes
    }
    assert set(by_key) == {"changed", "deleted", "added"}
    assert by_key["deleted"].operation == "delete"
    assert by_key["changed"].operation == "upsert"
    assert loads(by_key["changed"].payload) == 20
    assert by_key["added"].position == 2


def test_dirty_record_changes_cover_list_lifecycle_and_scalar_defaults():
    before = _State(events=[1, 2], committed_tick=1)
    after = _State(events=[1, 3, 4], committed_tick=2)

    changes = changes_for_dirty_records(
        before,
        after,
        {
            ("events", 0),
            ("events", 1),
            ("events", 2),
            ("events", 9),
            ("committed_tick", "__scalar__"),
        },
    )
    by_identity = {(change.collection, change.key): change for change in changes}
    assert ("events", "0") not in by_identity
    assert loads(by_identity[("events", "1")].payload) == 3
    assert loads(by_identity[("events", "2")].payload) == 4
    assert loads(by_identity[("committed_tick", "__scalar__")].payload) == 2

    deleted = changes_for_dirty_records(
        _State(events=[1, 2]),
        _State(events=[1]),
        {("events", 1)},
    )
    assert len(deleted) == 1
    assert deleted[0].operation == "delete"

    defaulted = changes_for_dirty_records(
        _State(committed_tick=5),
        _State(committed_tick=None),
        {("committed_tick", "__scalar__")},
    )
    assert loads(defaulted[0].payload) == -1
    assert changes_for_dirty_records(
        _State(committed_tick=5),
        _State(committed_tick=5),
        {("committed_tick", "__scalar__")},
    ) == ()


def _completed_trigger(**overrides):
    values = dict(
        trigger_id="trigger-1",
        requested_ticks=1,
        start_tick=0,
        end_tick=1,
        config_revision=1,
        logical_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        run_count=1,
    )
    values.update(overrides)
    return CompletedJobTrigger(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"trigger_id": ""}, "trigger_id cannot be empty"),
        ({"requested_ticks": 0}, "requested_ticks must be >= 1"),
        ({"start_tick": -1}, "invalid completed trigger tick range"),
        ({"start_tick": 2, "end_tick": 1}, "invalid completed trigger tick range"),
    ],
)
def test_completed_job_trigger_validates_invariants(overrides, message):
    with pytest.raises(ValueError, match=message):
        _completed_trigger(**overrides)
    assert _completed_trigger().end_tick == 1


def _job_state(**overrides):
    values = dict(
        job_id="job-1",
        domain_name="domain",
        config_json="{}",
        config_revision=1,
        status="ready",
        initialized=True,
        logical_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        next_tick=0,
    )
    values.update(overrides)
    return SimulationJobState(**values)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"job_id": ""}, "job_id cannot be empty"),
        ({"domain_name": ""}, "domain_name cannot be empty"),
        ({"config_revision": 0}, "config_revision must be >= 1"),
        ({"next_tick": -1}, "next_tick must be >= 0"),
        ({"run_count": -1}, "run_count must be >= 0"),
        ({"active_batch_total_ticks": -1}, "active_batch_total_ticks must be >= 0"),
        ({"active_batch_completed_ticks": -1}, "active_batch_completed_ticks must be >= 0"),
        (
            {"active_batch_total_ticks": 1, "active_batch_completed_ticks": 2},
            "active_batch_completed_ticks cannot exceed",
        ),
        ({"last_completed_batch_ticks": -1}, "last_completed_batch_ticks must be >= 0"),
        ({"status": "unknown"}, "unsupported job status"),
        ({"phase": "unknown"}, "unsupported job phase"),
    ],
)
def test_simulation_job_state_validates_invariants(overrides, message):
    with pytest.raises(ValueError, match=message):
        _job_state(**overrides)
    assert _job_state().status == "ready"


def _capabilities(**overrides):
    values = dict(
        process_durable=False,
        transactional_commits=False,
        incremental_updates=False,
        concurrent_writers=False,
        remote=False,
        analytical_reads=False,
        append_only=False,
        schema_migrations=False,
    )
    values.update(overrides)
    return PersistenceCapabilities(**values)


def test_persistence_registry_contract_and_factory_errors(tmp_path):
    registry = PersistenceRegistry()
    adapter = PersistenceAdapter(
        "demo",
        lambda options, base_dir: object(),
        _capabilities(transactional_commits=True),
    )
    registry.register(adapter)

    assert registry.names() == ("demo",)
    assert registry.adapter("demo") is adapter
    assert registry.describe() == (adapter,)
    assert registry.capabilities("demo").names() == ("transactional_commits",)
    assert registry.require("demo", "transactional_commits") is adapter

    with pytest.raises(ValueError, match="already registered"):
        registry.register(adapter)
    with pytest.raises(KeyError, match="unknown persistence adapter"):
        registry.adapter("missing")
    with pytest.raises(ValueError, match="lacks required capabilities"):
        registry.require("demo", "process_durable", "does_not_exist")

    missing_optional = PersistenceRegistry()
    missing_optional.register(
        PersistenceAdapter(
            "optional",
            lambda options, base_dir: (_ for _ in ()).throw(ModuleNotFoundError("dep")),
            _capabilities(),
            optional_extra="optional-extra",
        )
    )
    with pytest.raises(RuntimeError, match="requires optional extra"):
        missing_optional.create("optional", base_dir=tmp_path)

    raw_failure = PersistenceRegistry()
    raw_failure.register(
        PersistenceAdapter(
            "raw",
            lambda options, base_dir: (_ for _ in ()).throw(ModuleNotFoundError("dep")),
            _capabilities(),
        )
    )
    with pytest.raises(ModuleNotFoundError):
        raw_failure.create("raw", base_dir=tmp_path)


@pytest.mark.parametrize("raw", ["", 0, False])
def test_persistence_path_rejects_invalid_values(tmp_path, raw):
    with pytest.raises(ValueError, match="non-empty string"):
        resolve_persistence_path({"path": raw}, tmp_path, default="default.db")


def test_persistence_path_resolves_relative_and_absolute(tmp_path):
    assert resolve_persistence_path({}, tmp_path, default="default.db") == tmp_path / "default.db"
    absolute = tmp_path / "absolute.db"
    assert resolve_persistence_path({"path": str(absolute)}, Path("/ignored"), default="x") == absolute


def _fake_persistence_module(monkeypatch, name: str, class_name: str, constructor):
    module = ModuleType(name)
    setattr(module, class_name, constructor)
    monkeypatch.setitem(sys.modules, name, module)


def test_builtin_persistence_registry_validates_adapter_options(tmp_path, monkeypatch):
    registry = builtin_persistence_registry()
    assert set(registry.names()) == {
        "clickhouse",
        "duckdb",
        "ducklake",
        "jsonl",
        "memory",
        "postgres",
        "sqlite",
        "sqlite_incremental",
    }

    memory = registry.create("memory", base_dir=tmp_path)
    assert memory.committed_tick() == -1

    with pytest.raises(ValueError, match="namespace"):
        registry.create(
            "sqlite_incremental",
            {"namespace": ""},
            base_dir=tmp_path,
        )

    _fake_persistence_module(
        monkeypatch,
        "sose.persistence.postgres",
        "PostgresPersistence",
        lambda dsn, namespace: ("postgres", dsn, namespace),
    )
    for options, message in [
        ({"dsn": 3}, "dsn must be a non-empty string"),
        ({"dsn_env": ""}, "dsn_env must be a non-empty string"),
        ({}, "connection string is missing"),
        ({"dsn": "postgresql://example", "namespace": ""}, "namespace must be a non-empty string"),
    ]:
        monkeypatch.delenv("SOSE_DATABASE_URL", raising=False)
        with pytest.raises(ValueError, match=message):
            registry.create("postgres", options, base_dir=tmp_path)

    monkeypatch.setenv("CUSTOM_DSN", "postgresql://from-env")
    assert registry.create(
        "postgres",
        {"dsn_env": "CUSTOM_DSN", "namespace": "ns"},
        base_dir=tmp_path,
    ) == ("postgres", "postgresql://from-env", "ns")


def test_builtin_persistence_registry_covers_clickhouse_and_ducklake_factories(
    tmp_path, monkeypatch
):
    registry = builtin_persistence_registry()

    _fake_persistence_module(
        monkeypatch,
        "sose.persistence.clickhouse",
        "ClickHousePersistence",
        lambda **kwargs: ("clickhouse", kwargs),
    )
    for options, message in [
        ({"host": ""}, "host must be a non-empty string"),
        ({"database": ""}, "database must be a non-empty string"),
        ({"table": "bad-name"}, "table must be a simple identifier"),
    ]:
        with pytest.raises(ValueError, match=message):
            registry.create("clickhouse", options, base_dir=tmp_path)
    assert registry.create(
        "clickhouse",
        {"host": "db", "database": "analytics", "table": "sose_records"},
        base_dir=tmp_path,
    )[0] == "clickhouse"

    _fake_persistence_module(
        monkeypatch,
        "sose.persistence.ducklake",
        "DuckLakePersistence",
        lambda catalog, data_path, alias: ("ducklake", catalog, data_path, alias),
    )
    for options, message in [
        ({"data_path": ""}, "data_path must be a non-empty string"),
        ({"alias": ""}, "alias must be a non-empty string"),
    ]:
        with pytest.raises(ValueError, match=message):
            registry.create("ducklake", options, base_dir=tmp_path)

    result = registry.create(
        "ducklake",
        {"path": "catalog.ducklake", "data_path": "data", "alias": "lake"},
        base_dir=tmp_path,
    )
    assert result == (
        "ducklake",
        tmp_path / "catalog.ducklake",
        tmp_path / "data",
        "lake",
    )

    absolute_data = tmp_path / "absolute-data"
    result = registry.create(
        "ducklake",
        {"data_path": str(absolute_data)},
        base_dir=tmp_path,
    )
    assert result[2] == absolute_data


def test_sink_registry_contract_and_option_resolution(tmp_path, monkeypatch):
    registry = SinkRegistry()
    adapter = SinkAdapter("demo", lambda options, base_dir: ("sink", options, base_dir))
    registry.register(adapter)
    assert registry.names() == ("demo",)
    assert registry.adapter("demo") is adapter
    assert registry.create("demo", {"x": 1}, base_dir=tmp_path) == (
        "sink",
        {"x": 1},
        tmp_path,
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.register(adapter)
    with pytest.raises(KeyError, match="unknown sink adapter"):
        registry.adapter("missing")

    optional = SinkRegistry()
    optional.register(
        SinkAdapter(
            "optional",
            lambda options, base_dir: (_ for _ in ()).throw(ModuleNotFoundError("dep")),
            optional_extra="extra",
        )
    )
    with pytest.raises(RuntimeError, match="requires optional extra"):
        optional.create("optional", base_dir=tmp_path)

    raw = SinkRegistry()
    raw.register(
        SinkAdapter(
            "raw",
            lambda options, base_dir: (_ for _ in ()).throw(ModuleNotFoundError("dep")),
        )
    )
    with pytest.raises(ModuleNotFoundError):
        raw.create("raw", base_dir=tmp_path)

    monkeypatch.setenv("TOKEN_ENV", "secret")
    assert _option_or_env({"token": "direct"}, "token", "TOKEN_ENV") == "direct"
    assert _option_or_env({}, "token", "TOKEN_ENV") == "secret"
    assert _option_or_env({}, "optional", "MISSING", required=False) is None

    with pytest.raises(ValueError, match="must be a non-empty string"):
        _option_or_env({"token": ""}, "token", "TOKEN_ENV")
    with pytest.raises(ValueError, match="must be a non-empty string"):
        _option_or_env({"token_env": 3}, "token", "TOKEN_ENV")
    monkeypatch.delenv("MISSING_REQUIRED", raising=False)
    with pytest.raises(ValueError, match="is missing"):
        _option_or_env({}, "token", "MISSING_REQUIRED")


def test_sink_path_resolution(tmp_path):
    assert resolve_sink_path({}, tmp_path) == tmp_path / "analytics.jsonl"
    absolute = tmp_path / "absolute.jsonl"
    assert resolve_sink_path({"path": str(absolute)}, Path("/ignored")) == absolute
    for raw in ("", 0):
        with pytest.raises(ValueError, match="non-empty string"):
            resolve_sink_path({"path": raw}, tmp_path)


def test_builtin_sink_registry_factories_without_external_services(tmp_path, monkeypatch):
    registry = builtin_sink_registry()
    assert registry.names() == ("databricks", "jsonl", "snowflake")

    jsonl_sink = registry.create("jsonl", {"path": "events.jsonl"}, base_dir=tmp_path)
    assert jsonl_sink.path == tmp_path / "events.jsonl"

    databricks_calls = []
    databricks = ModuleType("databricks")
    databricks.sql = SimpleNamespace(
        connect=lambda **kwargs: databricks_calls.append(kwargs) or "db-connection"
    )
    monkeypatch.setitem(sys.modules, "databricks", databricks)

    options = {
        "server_hostname": "host",
        "http_path": "/sql/path",
        "access_token": "token",
        "events_table": "events",
        "batches_table": "batches",
    }
    sink = registry.create("databricks", options, base_dir=tmp_path)
    assert sink.events_table == "events"
    assert sink.connection_factory() == "db-connection"
    assert databricks_calls == [
        {
            "server_hostname": "host",
            "http_path": "/sql/path",
            "access_token": "token",
        }
    ]
    with pytest.raises(ValueError, match="table names must be strings"):
        registry.create(
            "databricks",
            {**options, "events_table": 3},
            base_dir=tmp_path,
        )

    snowflake_pkg = ModuleType("snowflake")
    connector = ModuleType("snowflake.connector")
    snowflake_calls = []
    connector.connect = lambda **kwargs: snowflake_calls.append(kwargs) or "sf-connection"
    snowflake_pkg.connector = connector
    monkeypatch.setitem(sys.modules, "snowflake", snowflake_pkg)
    monkeypatch.setitem(sys.modules, "snowflake.connector", connector)

    named = registry.create(
        "snowflake",
        {"connection_name": "named"},
        base_dir=tmp_path,
    )
    assert named.connection_factory() == "sf-connection"
    assert snowflake_calls[-1] == {"connection_name": "named"}

    with pytest.raises(ValueError, match="connection_name"):
        registry.create(
            "snowflake",
            {"connection_name": ""},
            base_dir=tmp_path,
        )

    password_options = {
        "account": "acct",
        "user": "user",
        "password": "pw",
        "warehouse": "wh",
        "database": "db",
        "schema": "public",
        "role": "role",
        "events_table": "EVENTS",
        "batches_table": "BATCHES",
    }
    password_sink = registry.create("snowflake", password_options, base_dir=tmp_path)
    assert password_sink.connection_factory() == "sf-connection"
    assert snowflake_calls[-1] == {
        "account": "acct",
        "user": "user",
        "password": "pw",
        "warehouse": "wh",
        "database": "db",
        "schema": "public",
        "role": "role",
    }

    minimal = registry.create(
        "snowflake",
        {"account": "acct", "user": "user", "password": "pw"},
        base_dir=tmp_path,
    )
    assert minimal.connection_factory() == "sf-connection"
    assert snowflake_calls[-1] == {
        "account": "acct",
        "user": "user",
        "password": "pw",
    }

    with pytest.raises(ValueError, match="table names must be strings"):
        registry.create(
            "snowflake",
            {"connection_name": "named", "batches_table": 7},
            base_dir=tmp_path,
        )
