from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sose.core.diagnostics import collect_runtime_diagnostics
from sose.examples.construction.scheduling import (
    PLANNED_START_DELAY,
    _planned_start_work,
    schedule_planned_start,
)
from sose.persistence.memory import _State
from sose.persistence.records import StateRecordChange


class _DiagnosticPersistence:
    def __init__(self, **collections):
        self._collections = collections

    def __getattr__(self, name):
        if name == "command":
            return lambda command_id: self._collections.get("commands", {}).get(command_id)
        if name in {"scenario_state", "simulation_position"}:
            return lambda: self._collections.get(name)
        return lambda *args, **kwargs: tuple(self._collections.get(name, ()))


def test_domain_catalog_module_constructs_catalog():
    from sose.domain.catalog import catalog

    assert catalog is not None


def test_runtime_diagnostics_empty_snapshot_is_healthy():
    diagnostics = collect_runtime_diagnostics(_DiagnosticPersistence())

    assert diagnostics.healthy
    assert diagnostics.issues == ()
    assert diagnostics.counts.events == 0
    assert diagnostics.counts.sink_deliveries_pending == 0
    assert diagnostics.counts.sink_deliveries_failed == 0


def test_runtime_diagnostics_reports_all_missing_reference_families():
    ns = SimpleNamespace
    persistence = _DiagnosticPersistence(
        scheduled_work=(ns(work_id="w1", command_id="missing"),),
        commands={},
        resource_definitions=(),
        resource_demands=(ns(request_id="rd", resource_name="resource-x"),),
        resource_reservations=(ns(reservation_id="rr", resource_name="resource-x"),),
        resource_release_intents=(ns(intent_id="ri", resource_name="resource-x"),),
        store_definitions=(),
        store_items=(ns(item_id="si", store_name="store-x"),),
        store_put_intents=(ns(item_id="spi", store_name="store-x"),),
        store_get_requests=(ns(request_id="sgr", store_name="store-x"),),
        store_get_results=(ns(request_id="sgo", store_name="store-x"),),
        container_definitions=(),
        container_states=(ns(name="container-x"),),
        container_operation_intents=(ns(request_id="ci", container_name="container-x"),),
        container_operation_results=(ns(request_id="co", container_name="container-x"),),
        preemptive_resource_definitions=(),
        preemptive_resource_demands=(ns(request_id="pd", resource_name="pre-x"),),
        preemptive_resource_reservations=(ns(reservation_id="pr", resource_name="pre-x"),),
        preemptive_resource_release_intents=(ns(intent_id="pi", resource_name="pre-x"),),
        resource_preemption_results=(ns(result_id="p-result"),),
        sink_deliveries=(
            ns(
                delivery_id="delivery-1",
                sink_name="warehouse",
                status="pending",
                last_error="boom",
            ),
            ns(
                delivery_id="delivery-2",
                sink_name="warehouse",
                status="delivered",
                last_error="old error ignored",
            ),
        ),
        events=(ns(), ns()),
        scenario_state=ns(decisions=(1, 2), activations=(1,)),
        simulation_position=ns(logical_tick=4),
    )

    diagnostics = collect_runtime_diagnostics(persistence)
    codes = [issue.code for issue in diagnostics.issues]

    assert not diagnostics.healthy
    assert codes.count("scheduled.command_missing") == 1
    assert codes.count("resource.definition_missing") == 3
    assert codes.count("store.definition_missing") == 4
    assert codes.count("container.definition_missing") == 3
    assert codes.count("preemptive.definition_missing") == 3
    assert codes.count("sink.delivery_failed") == 1
    assert diagnostics.counts.events == 2
    assert diagnostics.counts.scenario_decisions == 2
    assert diagnostics.counts.scenario_activations == 1
    assert diagnostics.counts.resource_preemption_results == 1
    assert diagnostics.counts.sink_deliveries_pending == 1
    assert diagnostics.counts.sink_deliveries_failed == 1
    assert diagnostics.position.logical_tick == 4


def test_runtime_diagnostics_accepts_known_references():
    ns = SimpleNamespace
    persistence = _DiagnosticPersistence(
        resource_definitions=(ns(name="r"),),
        resource_demands=(ns(request_id="d", resource_name="r"),),
        resource_reservations=(ns(reservation_id="res", resource_name="r"),),
        resource_release_intents=(ns(intent_id="rel", resource_name="r"),),
        store_definitions=(ns(name="s"),),
        store_items=(ns(item_id="i", store_name="s"),),
        store_put_intents=(ns(item_id="p", store_name="s"),),
        store_get_requests=(ns(request_id="g", store_name="s"),),
        store_get_results=(ns(request_id="gr", store_name="s"),),
        container_definitions=(ns(name="c"),),
        container_states=(ns(name="c"),),
        container_operation_intents=(ns(request_id="ci", container_name="c"),),
        container_operation_results=(ns(request_id="cr", container_name="c"),),
        preemptive_resource_definitions=(ns(name="p"),),
        preemptive_resource_demands=(ns(request_id="pd", resource_name="p"),),
        preemptive_resource_reservations=(ns(reservation_id="pr", resource_name="p"),),
        preemptive_resource_release_intents=(ns(intent_id="pi", resource_name="p"),),
    )

    assert collect_runtime_diagnostics(persistence).healthy


class _SchedulingPersistence:
    def __init__(self, activity, scheduled=(), commands=None):
        self._activity = activity
        self._scheduled = tuple(scheduled)
        self._commands = commands or {}

    def entity(self, entity_type, entity_id):
        assert entity_type == "construction_activity"
        return self._activity

    def scheduled_work(self):
        return self._scheduled

    def command(self, command_id):
        return self._commands.get(command_id)


class _FakeCommands:
    def __init__(self):
        self.created = []

    def create(self, name, **kwargs):
        command = SimpleNamespace(
            id="cmd-new",
            name=name,
            entity_type="construction_activity",
            entity_id=kwargs["target"].id,
            due_at=kwargs["due_at"],
        )
        self.created.append((name, kwargs, command))
        return command


class _FakeSchedules:
    def __init__(self):
        self.calls = []

    def at(self, due_at, *, command):
        self.calls.append((due_at, command))


def _engine():
    commands = _FakeCommands()
    schedules = _FakeSchedules()
    return SimpleNamespace(
        context=SimpleNamespace(commands=commands, schedules=schedules)
    )


def _entities():
    return SimpleNamespace(activity_id="activity-1")


def _activity(state="ready", *, staged=True):
    return SimpleNamespace(
        id="activity-1",
        state=state,
        attributes={"material_staged": staged},
    )


def test_construction_planned_start_guards_and_idempotency():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    backend = SimpleNamespace(now=now)
    engine = _engine()

    waiting = _SchedulingPersistence(_activity("waiting_resource"))
    assert schedule_planned_start(
        waiting, engine, backend, entities=_entities()
    ) == now

    with pytest.raises(RuntimeError, match="requires Activity"):
        schedule_planned_start(
            _SchedulingPersistence(_activity("in_progress")),
            engine,
            backend,
            entities=_entities(),
        )

    with pytest.raises(RuntimeError, match="material staging"):
        schedule_planned_start(
            _SchedulingPersistence(_activity(staged=False)),
            engine,
            backend,
            entities=_entities(),
        )

    existing_command = SimpleNamespace(
        entity_type="construction_activity",
        entity_id="activity-1",
        name="request_resources",
    )
    existing_work = SimpleNamespace(command_id="cmd-existing", due_at=now + timedelta(minutes=5))
    existing = _SchedulingPersistence(
        _activity(),
        scheduled=(existing_work,),
        commands={"cmd-existing": existing_command},
    )
    assert _planned_start_work(existing, "activity-1") == (existing_work, existing_command)
    assert schedule_planned_start(
        existing, engine, backend, entities=_entities()
    ) == existing_work.due_at


def test_construction_planned_start_creates_one_durable_schedule():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    backend = SimpleNamespace(now=now)
    engine = _engine()
    persistence = _SchedulingPersistence(_activity())

    due_at = schedule_planned_start(
        persistence,
        engine,
        backend,
        entities=_entities(),
    )

    assert due_at == now + PLANNED_START_DELAY
    assert len(engine.context.commands.created) == 1
    assert engine.context.commands.created[0][0] == "request_resources"
    assert engine.context.schedules.calls == [
        (due_at, engine.context.commands.created[0][2])
    ]


class _FakeDuckConnection:
    def __init__(self, schema_row=None):
        self.schema_row = schema_row
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return self.schema_row

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


def test_ducklake_initializes_catalog_and_schema(tmp_path, monkeypatch):
    import sose.persistence.ducklake as module

    connection = _FakeDuckConnection(schema_row=None)
    monkeypatch.setattr(module.duckdb, "connect", lambda path: connection)
    monkeypatch.setattr(module.DuckLakePersistence, "_refresh_from_db", lambda self: None)

    persistence = module.DuckLakePersistence(
        tmp_path / "catalog.ducklake",
        data_path=tmp_path / "data",
        alias="lake",
    )

    assert persistence.path == str(tmp_path / "catalog.ducklake")
    assert (tmp_path / "data").is_dir()
    statements = [sql for sql, _ in connection.executed]
    assert "INSTALL ducklake" in statements
    assert "LOAD ducklake" in statements
    assert any("ATTACH ? AS lake" in sql for sql in statements)
    assert any("INSERT INTO sose_record_meta VALUES (1, 1)" in sql for sql in statements)


def test_ducklake_rejects_unknown_schema_version(tmp_path, monkeypatch):
    import sose.persistence.ducklake as module

    connection = _FakeDuckConnection(schema_row=(2,))
    monkeypatch.setattr(module.duckdb, "connect", lambda path: connection)
    monkeypatch.setattr(module.DuckLakePersistence, "_refresh_from_db", lambda self: None)

    with pytest.raises(RuntimeError, match="unsupported DuckLakePersistence schema version"):
        module.DuckLakePersistence(
            tmp_path / "catalog.ducklake",
            data_path=tmp_path / "data",
        )


def test_ducklake_apply_changes_covers_upsert_delete_and_invalid_shapes(monkeypatch):
    import sose.persistence.ducklake as module

    persistence = object.__new__(module.DuckLakePersistence)
    persistence._connection = _FakeDuckConnection()

    before = _State(commands={"drop": 1, "change": 2})
    after = _State(commands={"change": 3, "add": 4})
    count = persistence._apply_changes(
        before,
        after,
        {("commands", "drop"), ("commands", "change"), ("commands", "add")},
    )
    assert count == 3
    assert any("INSERT INTO sose_record" in sql for sql, _ in persistence._connection.executed)

    monkeypatch.setattr(
        module,
        "changes_for_dirty_records",
        lambda *args: (
            StateRecordChange("upsert", "commands", '"x"', None, None),
        ),
    )
    with pytest.raises(RuntimeError, match="requires position and payload"):
        persistence._apply_changes(_State(), _State(), set())

    monkeypatch.setattr(
        module,
        "changes_for_dirty_records",
        lambda *args: (
            StateRecordChange("mystery", "commands", '"x"'),
        ),
    )
    with pytest.raises(RuntimeError, match="unknown state record operation"):
        persistence._apply_changes(_State(), _State(), set())
