from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 4, 1, tzinfo=timezone.utc)


def processor_outage_scenario() -> Scenario:
    return Scenario(
        name="cards-processor-outage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=2),
        effects=(AttributeEffect("payments.processor.available", False),),
    )


def fraud_pressure_scenario() -> Scenario:
    return Scenario(
        name="cards-fraud-pressure",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=6),
        effects=(AttributeEffect("payments.authorization.risk_multiplier", 2.0),),
    )
