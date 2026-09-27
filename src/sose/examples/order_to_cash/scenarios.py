from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 8, 1, tzinfo=timezone.utc)


def credit_tightening_scenario() -> Scenario:
    return Scenario(
        name="o2c-credit-tightening",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=8),
        effects=(AttributeEffect("o2c.credit.available", False),),
    )


def fulfillment_capacity_loss_scenario() -> Scenario:
    return Scenario(
        name="o2c-fulfillment-capacity-loss",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=6),
        effects=(AttributeEffect("o2c.fulfillment.available", False),),
    )
