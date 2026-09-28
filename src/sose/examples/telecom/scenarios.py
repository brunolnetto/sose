from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def provisioning_outage_scenario() -> Scenario:
    return Scenario(
        name="telecom-provisioning-outage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("telecom.provisioning.available", False),),
    )
