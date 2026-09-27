from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def hub_congestion_scenario() -> Scenario:
    return Scenario(
        name="logistics-hub-congestion",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=6),
        effects=(AttributeEffect("logistics.hub.capacity.factor", 0.5),),
    )


def courier_capacity_loss_scenario() -> Scenario:
    return Scenario(
        name="logistics-courier-capacity-loss",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("logistics.courier.available", False),),
    )


def weather_delay_scenario() -> Scenario:
    return Scenario(
        name="logistics-weather-delay",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=8),
        effects=(AttributeEffect("logistics.transfer.delay_factor", 2.0),),
    )
