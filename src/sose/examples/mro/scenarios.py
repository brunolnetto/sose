from __future__ import annotations

from datetime import timedelta

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger

from .simulation import ORIGIN


def asset_failure_scenario() -> Scenario:
    return Scenario(
        name="mro-asset-failure",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=3),
        effects=(AttributeEffect("mro.asset.emergency", True),),
    )


def spare_parts_disruption_scenario() -> Scenario:
    return Scenario(
        name="mro-spare-parts-disruption",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("mro.spare_parts.available", False),),
    )


def technician_capacity_loss_scenario() -> Scenario:
    return Scenario(
        name="mro-technician-capacity-loss",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("mro.technician.available", False),),
    )
