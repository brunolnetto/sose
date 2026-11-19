"""ScenarioRule and the Engine rules_for hook are deprecated in 0.11.0 (#634, ADR-0011)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry
from sose.persistence.memory import MemoryPersistence
from sose.scenarios.rules import ScenarioRule

NOW = datetime(2026, 1, 1)


def _engine(**options) -> Engine:
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(hours=1)),
        random=RandomSource(root_seed=1),
        scheduler=Scheduler(),
    )
    return Engine(
        context=context, registry=DomainRegistry(), persistence=MemoryPersistence(),
        owner_id="owner", **options,
    )


def _message(record: pytest.WarningsRecorder) -> str:
    (warning,) = [w for w in record if issubclass(w.category, DeprecationWarning)]
    assert warning.filename == __file__, "the warning must point at the calling code"
    return str(warning.message)


def test_constructing_a_scenario_rule_warns_at_the_caller():
    with pytest.warns(DeprecationWarning) as record:
        ScenarioRule("rule", lambda entity, context: True, lambda entity, context: None)
    message = _message(record)
    assert "ScenarioRule is deprecated since SOSE 0.11.0" in message
    assert "removed in SOSE 0.12.0" in message
    assert "Scenario and ScenarioEngine" in message


def test_engine_rules_for_hook_warns_at_the_caller():
    with pytest.warns(DeprecationWarning) as record:
        _engine(rules_for=lambda entity_type: ())
    assert "Engine(rules_for=...) is deprecated since SOSE 0.11.0" in _message(record)


def test_engine_without_rules_for_does_not_warn(recwarn):
    _engine()
    assert not [w for w in recwarn if issubclass(w.category, DeprecationWarning)]


def test_evaluate_entity_warns_at_the_caller():
    engine = _engine()
    entity = type("Entity", (), {"entity_type": "none"})()  # no rules are registered
    with pytest.warns(DeprecationWarning) as record:
        engine.evaluate_entity(entity)
    assert "Engine.evaluate_entity is deprecated since SOSE 0.11.0" in _message(record)
