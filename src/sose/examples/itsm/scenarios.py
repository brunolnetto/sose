from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 5, 1, tzinfo=timezone.utc)


def incident_storm_scenario() -> Scenario:
    return Scenario(
        name="itsm-incident-storm",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=6),
        effects=(AttributeEffect("itsm.incident.arrival_multiplier", 3.0),),
    )


def staff_shortage_scenario() -> Scenario:
    return Scenario(
        name="itsm-staff-shortage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("itsm.support.available", False),),
    )
