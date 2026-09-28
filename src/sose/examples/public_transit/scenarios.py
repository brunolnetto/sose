from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 12, 1, 8, tzinfo=timezone.utc)


def vehicle_tracking_outage() -> tuple[Scenario, Scenario]:
    return (
        Scenario(
            name="vehicle-tracking-outage",
            trigger=ScheduledTrigger(at=ORIGIN),
            effects=(AttributeEffect("transit.tracking.available", False),),
        ),
        Scenario(
            name="vehicle-tracking-restored",
            trigger=ScheduledTrigger(at=ORIGIN + timedelta(minutes=2)),
            effects=(AttributeEffect("transit.tracking.available", True),),
        ),
    )
