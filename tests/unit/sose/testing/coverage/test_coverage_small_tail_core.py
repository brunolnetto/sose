from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import sose.factories.statechart as statechart_module
import sose.jobs.config as job_config
import sose.persistence.clickhouse as clickhouse_module
from sose.core.clock import SimulationClock
from sose.core.events import Command
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.entity import Entity
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.warehouse import DomainMutation, MemoryDomainWarehouse
from sose.factories.entity import EntityFactory
from sose.factories.event import EventFactory
from sose.factories.schedule import ScheduleFactory
from sose.factories.statechart import StateChartFactory
from sose.jobs.config import SOSEConfig, engine_namespace_for_job
from sose.jobs.storage import build_storage_plan
from sose.persistence.clickhouse import ClickHousePersistence
from sose.persistence.memory import MemoryPersistence
from sose.persistence.qualification import (
    PersistenceQualification,
    PersistenceTier,
)
from sose.persistence.registry import builtin_persistence_registry
from sose.scenarios.engine import ScenarioEngine
from sose.scenarios.model import ScenarioActivation, TransitionWeightEffect
from sose.sinks.jsonl import JSONLAnalyticalSink


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _command() -> Command:
    return Command(
        command_id="command-1",
        name="run",
        entity_type="thing",
        entity_id="thing-1",
        due_at=NOW,
    )


def _entity() -> Entity:
    return Entity(
        id="thing-1",
        entity_type="thing",
        created_at=NOW,
        updated_at=NOW,
    )


def test_scheduler_length_exposes_pending_count():
    scheduler = Scheduler()

    assert len(scheduler) == 0
    scheduler.schedule(_command())
    assert len(scheduler) == 1


def test_clock_rejects_non_positive_advance():
    clock = SimulationClock(now=NOW, step=timedelta(minutes=1))

    with pytest.raises(ValueError, match="steps must be >= 1"):
        clock.advance(0)


def test_event_factory_builds_exception_event():
    events = EventFactory(now=lambda: NOW, tick=lambda: 7)

    event = events.exception("timeout", entity=_entity(), reason="slow")

    assert event.name == "exception.timeout"
    assert event.tick == 7
    assert event.payload == {"reason": "slow"}


def test_statechart_graph_is_cached_by_chart_type(monkeypatch):
    calls = []
    sentinel = object()
    monkeypatch.setattr(
        statechart_module,
        "graph_from_statechart",
        lambda chart: calls.append(chart) or sentinel,
    )
    factory = StateChartFactory(
        registry=None,
        events=None,
        emit=lambda event: None,
        now=lambda: NOW,
        transition_runtime=None,
        context=lambda: None,
    )

    class Chart:
        pass

    first = Chart()
    second = Chart()
    assert factory.graph(first) is sentinel
    assert factory.graph(second) is sentinel
    assert calls == [first]


def test_entity_factory_rejects_empty_business_key():
    factory = EntityFactory(now=lambda: NOW)

    with pytest.raises(ValueError, match="entity key must contain"):
        factory.create(Entity, key=(), entity_type="thing")


def test_schedule_factory_rejects_negative_delay():
    schedules = ScheduleFactory(now=lambda: NOW, scheduler=Scheduler())

    with pytest.raises(ValueError, match="delay cannot be negative"):
        schedules.after(timedelta(seconds=-1), command=_command())


def test_engine_namespace_guard_rejects_unexpected_derived_identifier(monkeypatch):
    class RejectAll:
        def fullmatch(self, value):
            return None

    monkeypatch.setattr(job_config, "_ENGINE_NAMESPACE_RE", RejectAll())

    with pytest.raises(ValueError, match="cannot derive Engine Store namespace"):
        engine_namespace_for_job("valid-job")


def test_append_only_authoritative_store_is_reported():
    config = SOSEConfig.model_validate(
        {
            "domain": {"name": "tutorial_job"},
            "engine_store": {"adapter": "jsonl"},
            "job": {"id": "coverage-job"},
        }
    )

    plan = build_storage_plan(config)

    assert any(
        issue.code == "storage.authoritative_append_only"
        for issue in plan.issues
    )


def test_persistence_qualification_requires_suite_version():
    with pytest.raises(ValueError, match="suite_version must be non-empty"):
        PersistenceQualification(
            tier=PersistenceTier.DURABLE,
            suite_version="",
        )


def test_scenario_transition_multiplier_ignores_other_entity_types():
    engine = ScenarioEngine(
        random_source=RandomSource(7),
        now=lambda: NOW,
        tick=lambda: 0,
        context=lambda: None,
    )
    activation = ScenarioActivation(
        activation_id="activation-1",
        scenario_name="pricing",
        activated_at=NOW,
        expires_at=None,
        priority=100,
        effects=(
            TransitionWeightEffect(
                event="approve",
                multiplier=2.0,
                entity_type="order",
            ),
        ),
        trigger_key=("tick", 0),
    )
    engine._activations[activation.activation_id] = activation

    assert engine.transition_weight_multiplier("invoice", "approve") == 1.0


def test_jsonl_sink_skips_blank_lines_when_scanning(tmp_path):
    sink = JSONLAnalyticalSink(tmp_path / "analytics.jsonl")
    sink.path.write_text("\n   \n", encoding="utf-8")

    assert sink._contains("missing-batch") is False


def test_duckdb_registry_factory_is_exercised(tmp_path):
    registry = builtin_persistence_registry()
    persistence = registry.create(
        "duckdb",
        {"path": "coverage.duckdb"},
        base_dir=tmp_path,
    )
    try:
        assert persistence.path == str(tmp_path / "coverage.duckdb")
    finally:
        persistence.close()


def test_domain_outbox_reuses_identical_pending_mutation():
    persistence = MemoryPersistence()
    outbox = DomainMutationOutbox(persistence, MemoryDomainWarehouse())
    mutation = DomainMutation("mutation-1", _entity())

    first = outbox.prepare(mutation)
    second = outbox.prepare(mutation)

    assert second == first


def test_clickhouse_empty_snapshot_does_not_insert(monkeypatch):
    class Client:
        def __init__(self):
            self.inserts = []

        def insert(self, *args, **kwargs):
            self.inserts.append((args, kwargs))

    client = Client()
    persistence = ClickHousePersistence.__new__(ClickHousePersistence)
    persistence._client = client
    persistence._table = "sose_record_snapshot"
    persistence._state = object()
    monkeypatch.setattr(clickhouse_module, "state_to_records", lambda state: {})

    persistence._append_snapshot()

    assert client.inserts == []
