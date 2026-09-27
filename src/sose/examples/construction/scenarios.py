from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)


def weather_delay_scenario() -> Scenario:
    return Scenario(
        name="construction-weather-delay",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=8),
        effects=(AttributeEffect("construction.site.available", False),),
    )


def procurement_delay_scenario() -> Scenario:
    return Scenario(
        name="construction-procurement-delay",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=6),
        effects=(AttributeEffect("construction.material.available", False),),
    )
