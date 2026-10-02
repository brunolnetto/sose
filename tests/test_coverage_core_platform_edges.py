from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from sose.jobs.config import JobSection
from sose.jobs.config_edit import _write_parameter, parse_cli_value
from sose.jobs.job_edit import _write_job_section
from sose.jobs.scaffold import _schema_comment, _toml_value, render_sose_toml


def test_job_section_writer_rejects_missing_section():
    job = JobSection(id="job", ticks_per_trigger=2, max_ticks_per_trigger=4)
    with pytest.raises(ValueError, match=r"missing \[job\]"):
        _write_job_section("[domain]\nname = \"x\"\n", job)


def test_job_section_writer_replaces_and_inserts_before_next_section():
    job = JobSection(id="new-job", ticks_per_trigger=2, max_ticks_per_trigger=4)
    text = """[job]
  id = "old"

[next]
value = 1
"""
    rendered = _write_job_section(text, job)

    assert '  id = "new-job"' in rendered
    assert "ticks_per_trigger = 2" in rendered
    assert "max_ticks_per_trigger = 4" in rendered
    assert rendered.index("max_ticks_per_trigger = 4") < rendered.index("[next]")


def test_parameter_writer_creates_replaces_and_inserts(tmp_path):
    path = tmp_path / "sose.toml"
    path.write_text("[domain]\nname = \"x\"\n", encoding="utf-8")
    _write_parameter(path, "alpha", 1)
    assert "[domain.parameters]" in path.read_text(encoding="utf-8")
    assert "alpha = 1" in path.read_text(encoding="utf-8")

    path.write_text(
        """[domain.parameters]
  alpha = 1

[next]
value = 2
""",
        encoding="utf-8",
    )
    _write_parameter(path, "alpha", 3)
    assert "  alpha = 3" in path.read_text(encoding="utf-8")

    _write_parameter(path, "beta", True)
    rendered = path.read_text(encoding="utf-8")
    assert "beta = true" in rendered
    assert rendered.index("beta = true") < rendered.index("[next]")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("42", 42),
        ('{"x": 1}', {"x": 1}),
        ("plain-text", "plain-text"),
    ],
)
def test_parse_cli_value_json_or_text(raw, expected):
    assert parse_cli_value(raw) == expected


def test_toml_value_covers_collection_and_error_shapes():
    assert _toml_value([1, "x", False]) == '[1, "x", false]'
    assert _toml_value({"a": 1, "skip": None}) == "{ a = 1 }"
    assert _toml_value(1.5) == "1.5"
    with pytest.raises(TypeError, match="unsupported TOML scaffold value"):
        _toml_value(object())


def test_schema_comment_resolves_refs_titles_enums_and_bounds():
    defs = {
        "Choice": {
            "title": "Choice title",
            "type": "integer",
            "enum": [1, 2],
            "minimum": 0,
            "exclusiveMinimum": -1,
            "maximum": 3,
            "exclusiveMaximum": 4,
        }
    }
    comments = _schema_comment({"$ref": "#/$defs/Choice"}, defs)
    assert comments == [
        "Choice title",
        "type=integer; choices=1, 2; min=0; > -1; max=3; < 4",
    ]
    assert _schema_comment({"description": "  Detailed  "}, {}) == ["Detailed"]


class _FakeDefinition:
    name = "demo"

    def describe_config(self):
        return {
            "defaults": {
                "enabled": True,
                "count": 2,
                "optional": None,
            },
            "parameters": [
                {
                    "name": "enabled",
                    "mutability": "runtime",
                    "schema": {"description": "Enable demo", "type": "boolean"},
                },
                {
                    "name": "count",
                    "mutability": "restart",
                    "schema": {"title": "Count", "minimum": 1},
                },
            ],
            "$defs": {},
        }


def test_render_sose_toml_rejects_non_simpy_backend():
    with pytest.raises(ValueError, match="SimPy"):
        render_sose_toml(_FakeDefinition(), runtime_backend="other")


def test_render_sose_toml_postgres_namespace_normalization_and_hashing():
    text = render_sose_toml(
        _FakeDefinition(),
        job_id="9-" + ("Very Long Job Id!" * 5),
        persistence_adapter="postgres",
    )
    namespace_line = next(
        line for line in text.splitlines() if line.startswith("namespace = ")
    )
    namespace = namespace_line.split("=", 1)[1].strip().strip('"')

    assert namespace.startswith("job_")
    assert len(namespace) <= 40
    assert 'dsn_env = "SOSE_DATABASE_URL"' in text
    assert "# Enable demo" in text
    assert "# mutability=runtime" in text
    assert "optional" not in text


def test_render_sose_toml_file_adapter_uses_path():
    text = render_sose_toml(
        _FakeDefinition(),
        persistence_adapter="sqlite_incremental",
        persistence_path="state/custom.sqlite3",
    )
    assert 'path = "state/custom.sqlite3"' in text


def test_readers_writers_config_requires_writer():
    from sose.examples.canonical.readers_writers import ReadersWritersConfig

    with pytest.raises(ValidationError, match="readers must be less"):
        ReadersWritersConfig(participants=3, readers=3)


def test_resolve_tick_action_reuses_pending_action():
    from sose.examples.canonical.common import ACTION_STORE, resolve_tick_action

    persistence = SimpleNamespace(
        store_items=lambda: (),
        store_put_intents=lambda: (
            SimpleNamespace(
                store_name=ACTION_STORE,
                item_id="demo:tick:7",
                value={"action": "persisted-choice"},
            ),
        ),
    )
    result = resolve_tick_action(
        persistence,
        backend=object(),
        canonical="demo",
        logical_tick=7,
        candidate="new-choice",
        requested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert result == "persisted-choice"


class _FakeResources:
    def __init__(self, reservation=None):
        self._reservation = reservation
        self.requests = []
        self.releases = []

    def reservation_for(self, request_id):
        return self._reservation if (
            self._reservation is not None
            and self._reservation.request_id == request_id
        ) else None

    def ensure_requested(self, backend, **kwargs):
        self.requests.append(kwargs)

    def release(self, backend, reservation_id):
        self.releases.append(reservation_id)

    def has_request(self, request_id):
        return False


def _fake_engine(resources):
    return SimpleNamespace(
        resources=resources,
        context=SimpleNamespace(
            clock=SimpleNamespace(
                tick=1,
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ),
    )


def test_job_shop_noop_and_wrong_state_ready_action(monkeypatch):
    import sose.examples.canonical.job_shop as module

    current = SimpleNamespace(id="case", state="completed")
    persistence = SimpleNamespace(
        entity=lambda *args: current,
        store_items=lambda: (),
    )
    resources = _FakeResources()
    captured = {}
    monkeypatch.setattr(
        module,
        "resolve_tick_action",
        lambda *args, **kwargs: captured.setdefault("candidate", kwargs["candidate"]),
    )
    monkeypatch.setattr(
        module,
        "DurableStoreManager",
        lambda persistence: SimpleNamespace(ensure_put=lambda *a, **k: None),
    )

    module.reconcile(
        persistence,
        _fake_engine(resources),
        object(),
        module.JobShopConfig(participants=1, machines=1),
        SimpleNamespace(id="case"),
    )
    assert captured["candidate"] == "noop"

    current.state = "active"
    monkeypatch.setattr(module, "resolve_tick_action", lambda *a, **k: "ready")
    module.reconcile(
        persistence,
        _fake_engine(resources),
        object(),
        module.JobShopConfig(participants=1, machines=1),
        SimpleNamespace(id="case"),
    )
    assert resources.requests == []


def test_readers_writers_active_noop_finishes_when_queue_empty(monkeypatch):
    import sose.examples.canonical.readers_writers as module

    current = SimpleNamespace(id="case", state="active")
    persistence = SimpleNamespace(
        entity=lambda *args: current,
        resource_reservations=lambda: (),
        resource_demands=lambda: (),
    )
    transitions = []
    monkeypatch.setattr(module, "resolve_tick_action", lambda *a, **k: "active:noop")
    monkeypatch.setattr(
        module,
        "transition",
        lambda engine, case, event: transitions.append(event),
    )

    module.reconcile(
        persistence,
        _fake_engine(_FakeResources()),
        object(),
        module.ReadersWritersConfig(participants=2, readers=1),
        SimpleNamespace(id="case"),
    )
    assert transitions == ["finish"]


def test_sleeping_barber_active_action_ignored_when_state_changed(monkeypatch):
    import sose.examples.canonical.sleeping_barber as module

    current = SimpleNamespace(id="case", state="completed")
    persistence = SimpleNamespace(
        entity=lambda *args: current,
        resource_reservations=lambda: (),
        resource_demands=lambda: (),
    )
    monkeypatch.setattr(module, "resolve_tick_action", lambda *a, **k: "active:customer-0")

    resources = _FakeResources(
        SimpleNamespace(request_id="customer-0", reservation_id="res-1")
    )
    module.reconcile(
        persistence,
        _fake_engine(resources),
        object(),
        module.SleepingBarberConfig(participants=1, waiting_chairs=0),
        SimpleNamespace(id="case"),
    )
    assert resources.releases == []



class _PlainEnum(Enum):
    VALUE = "value"


def test_persistence_codec_round_trips_plain_enum():
    from sose.persistence.codec import dumps, loads

    assert loads(dumps(_PlainEnum.VALUE)) is _PlainEnum.VALUE


def _doctor_config():
    return SimpleNamespace(
        job=SimpleNamespace(id="job-1"),
        domain=SimpleNamespace(name="expected-domain"),
        persistence=SimpleNamespace(adapter="memory"),
    )


def _doctor_state(**overrides):
    values = {
        "job_id": "job-1",
        "domain_name": "expected-domain",
        "config_json": '{"enabled": true}',
        "config_revision": 2,
        "status": "ready",
        "initialized": True,
        "logical_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "next_tick": 3,
        "phase": "idle",
        "last_error": None,
        "active_trigger_id": None,
        "active_batch_trigger_id": None,
        "active_batch_completed_ticks": 0,
        "active_batch_total_ticks": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _DoctorPersistence:
    def __init__(self, state, *, deliveries=(), position=None):
        self.state = state
        self.deliveries = tuple(deliveries)
        self.position = position

    def job_state(self, job_id):
        return self.state

    def sink_deliveries(self, *, job_id=None):
        return self.deliveries

    def simulation_position(self):
        return self.position


def test_job_doctor_uninitialized_job_is_healthy(monkeypatch):
    import sose.jobs.doctor as module

    monkeypatch.setattr(
        module,
        "collect_runtime_diagnostics",
        lambda persistence: SimpleNamespace(issues=()),
    )
    report = module.inspect_open_job_health(
        _doctor_config(),
        persistence=_DoctorPersistence(None),
        resolved_config_json='{"enabled": true}',
    )

    assert report.healthy
    assert not report.initialized
    assert report.config_revision is None
    assert report.to_dict()["job_id"] == "job-1"


def test_job_doctor_reports_state_delivery_and_position_failures(monkeypatch):
    import sose.jobs.doctor as module

    monkeypatch.setattr(
        module,
        "collect_runtime_diagnostics",
        lambda persistence: SimpleNamespace(
            issues=(SimpleNamespace(code="runtime.issue", message="runtime problem"),)
        ),
    )
    state = _doctor_state(
        domain_name="other-domain",
        config_json="{broken",
        status="failed",
        last_error=None,
        active_trigger_id="trigger-1",
        active_batch_trigger_id="batch-1",
        active_batch_completed_ticks=1,
        active_batch_total_ticks=4,
    )
    deliveries = (
        SimpleNamespace(
            delivery_id="d1",
            sink_name="warehouse",
            status="pending",
            last_error=None,
        ),
        SimpleNamespace(
            delivery_id="d2",
            sink_name="warehouse",
            status="pending",
            last_error="network",
        ),
        SimpleNamespace(
            delivery_id="d3",
            sink_name="warehouse",
            status="delivered",
            last_error=None,
        ),
    )
    report = module.inspect_open_job_health(
        _doctor_config(),
        persistence=_DoctorPersistence(state, deliveries=deliveries, position=None),
        resolved_config_json='{"enabled": true}',
    )

    codes = [issue.code for issue in report.issues]
    assert codes.count("sink.delivery_pending") == 2
    assert "runtime.issue" in codes
    assert "job.domain_mismatch" in codes
    assert "job.config_invalid" in codes
    assert "job.failed" in codes
    assert "job.trigger_unresolved" in codes
    assert "job.batch_trigger_unresolved" in codes
    assert "job.position_missing" in codes


def test_job_doctor_reports_config_tick_and_time_drift(monkeypatch):
    import sose.jobs.doctor as module

    monkeypatch.setattr(
        module,
        "collect_runtime_diagnostics",
        lambda persistence: SimpleNamespace(issues=()),
    )
    state = _doctor_state(config_json='{"enabled": false}')
    position = SimpleNamespace(
        logical_tick=state.next_tick + 1,
        logical_time=state.logical_time + timedelta(minutes=1),
    )
    report = module.inspect_open_job_health(
        _doctor_config(),
        persistence=_DoctorPersistence(state, position=position),
        resolved_config_json='{"enabled": true}',
    )

    codes = {issue.code for issue in report.issues}
    assert codes == {
        "job.config_drift",
        "job.tick_mismatch",
        "job.time_mismatch",
    }
