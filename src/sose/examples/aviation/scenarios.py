from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 12, 1, 8, tzinfo=timezone.utc)


def departure_weather_scenario() -> Scenario:
    return Scenario(
        name="aviation-departure-weather",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=3),
        effects=(AttributeEffect("aviation.departure.available", False),),
    )


def crew_shortage_scenario() -> Scenario:
    return Scenario(
        name="aviation-crew-shortage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=2),
        effects=(AttributeEffect("aviation.crew.available", False),),
    )
