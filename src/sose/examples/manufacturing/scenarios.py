from __future__ import annotations

from datetime import timedelta

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger

from .simulation import ORIGIN


def machine_downtime_scenario() -> Scenario:
    return Scenario(
        name="manufacturing-machine-downtime",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=3),
        effects=(AttributeEffect("manufacturing.machine.down", True),),
    )


def yield_degradation_scenario() -> Scenario:
    return Scenario(
        name="manufacturing-yield-degradation",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("manufacturing.yield.factor", 0.8),),
    )


def demand_surge_scenario() -> Scenario:
    return Scenario(
        name="manufacturing-demand-surge",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("manufacturing.demand.multiplier", 2.0),),
    )
