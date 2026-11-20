"""Surfaces deprecated in 0.11.0 and removed in 0.12.0 (ADR-0011)."""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry
from sose.persistence.memory import MemoryPersistence


@pytest.mark.parametrize("module", [
    "sose.scenarios.rules",                    # ScenarioRule (#634)
    "sose.organizational.od2_legacy_audit",    # legacy OD-2 audit (#634)
])
def test_removed_modules_no_longer_import(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_engine_rules_hook_was_removed():
    context = SimulationContext(
        clock=SimulationClock(now=datetime(2026, 1, 1), step=timedelta(hours=1)),
        random=RandomSource(root_seed=1), scheduler=Scheduler(),
    )
    with pytest.raises(TypeError, match="rules_for"):
        Engine(context=context, registry=DomainRegistry(), persistence=MemoryPersistence(),
               owner_id="owner", rules_for=lambda entity_type: ())
    assert not hasattr(Engine, "evaluate_entity")
