from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.scenarios import AttributeEffect, Scenario, ScheduledTrigger


ORIGIN = datetime(2026, 12, 8, 8, tzinfo=timezone.utc)


def technician_dispatch_outage() -> Scenario:
    return Scenario(
        name="field-service-dispatch-outage",
        trigger=ScheduledTrigger(at=ORIGIN),
        duration=timedelta(hours=2),
        effects=(AttributeEffect("field_service.dispatch.available", False),),
    )
