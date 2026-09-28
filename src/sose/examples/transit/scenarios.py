from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 11, 3, 8, tzinfo=timezone.utc)


def realtime_feed_outage() -> Scenario:
    return Scenario(
        name="transit-realtime-feed-outage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=2),
        effects=(AttributeEffect("transit.realtime.available", False),),
    )
