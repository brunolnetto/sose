from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 9, 1, tzinfo=timezone.utc)


def close_team_shortage_scenario() -> Scenario:
    return Scenario(
        name="r2r-close-team-shortage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=4),
        effects=(AttributeEffect("r2r.close_team.available", False),),
    )


def posting_outage_scenario() -> Scenario:
    return Scenario(
        name="r2r-posting-outage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=3),
        effects=(AttributeEffect("r2r.posting.available", False),),
    )
