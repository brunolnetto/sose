from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 11, 1, tzinfo=timezone.utc)


def gate_congestion_scenario() -> Scenario:
    return Scenario(
        name="airport-gate-congestion",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=3),
        effects=(AttributeEffect("airport.gate.available", False),),
    )


def weather_departure_scenario() -> Scenario:
    return Scenario(
        name="airport-weather-departure-delay",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("airport.departure.available", False),),
    )
