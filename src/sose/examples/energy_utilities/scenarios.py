from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)


def demand_response_communications_outage() -> Scenario:
    return Scenario(
        name="utility-dr-communications-outage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=2),
        effects=(AttributeEffect("energy.dr.available", False),),
    )
